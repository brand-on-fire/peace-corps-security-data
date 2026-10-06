#!/usr/bin/env python3
"""Validate complete public-source batches and atomically replace local public data.

No fetch, credentials, commit, push or deployment. An offline dry-run is default.
Source-event equivalence is exact provider identity or an explicit reviewed alias;
headlines, nearby points, or publication dates never establish duplicate events.
"""
import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

import public_data_pipeline as pipeline

CATEGORIES={'earthquake','weather','flood','volcano','conflict','unrest','health','transport','crime','infrastructure','other'}
STATES={'ongoing','reported','expired','resolved','cancelled'}
MAX_SNAPSHOT=10_000_000
MAX_PART=12_000_000

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def timestamp(value):
    if not isinstance(value,str):raise ValueError('Timestamp must be a string')
    parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('Timestamp has no timezone')
    return parsed.astimezone(dt.timezone.utc)

def validate_record(row,countries,sources,areas,now):
    for key in ('id','title','summary','countryCode','region'):
        if not isinstance(row.get(key),str) or not row[key]:raise ValueError('Missing incident '+key)
    if row['countryCode'] not in countries:raise ValueError('New/changed incident outside reviewed active-country register')
    if row.get('category') not in CATEGORIES or row.get('state') not in STATES:raise ValueError('Invalid incident category/state')
    if row.get('severity') not in ('unknown','minor','moderate','severe'):raise ValueError('Invalid severity')
    if row.get('evidenceStatus') not in ('official','reported','unverified','corroborated'):raise ValueError('Invalid evidence status')
    if row.get('aiStatus') not in ('not-requested','pending','enriched','deferred'):raise ValueError('Invalid AI status')
    if not isinstance(row.get('historical'),bool):raise ValueError('Missing historical flag')
    for key in ('occurredAt','updatedAt','firstSeenAt'):timestamp(row[key])
    if timestamp(row['occurredAt'])>now+dt.timedelta(minutes=5):raise ValueError('Future event anchor exceeds reader limit; preserve planned start in metadata')
    location=row.get('location',{})
    if location.get('precision') not in ('point','area','country','unknown'):raise ValueError('Invalid geographic precision')
    if location['precision']=='point':
        for key,bound in [('lon',180),('lat',90)]:
            v=location.get(key)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not -bound<=v<=bound:raise ValueError('Invalid incident point')
    elif ('lat' in location or 'lon' in location):raise ValueError('Coordinates attached to non-point precision')
    for level in (1,2):
        a,b=row.get(f'admin{level}Id'),row.get(f'admin{level}Code')
        if a and b and a!=b:raise ValueError('Administrative aliases conflict')
        aid=a or b
        if aid:
            area=areas.get(aid)
            if not area or area['countryCode']!=row['countryCode'] or area['level']!=f'ADM{level}':raise ValueError('Unknown or cross-country administrative ID')
            if row.get(f'admin{level}Name')!=area['name']:raise ValueError('Administrative name/ID mismatch')
            if level==2 and area['parentId']!=(row.get('admin1Code') or row.get('admin1Id')):raise ValueError('Administrative parent conflict: '+row['id']+' / '+str(aid)+' / '+str(row.get('admin1Code') or row.get('admin1Id')))
    assignment=row.get('adminAssignment',{})
    if assignment.get('method')=='strict_point_containment':
        for name,level in assignment.get('levels',{}).items():
            if level.get('status')!='unavailable' and not isinstance(level.get('source'),dict) and not all(level.get(k) for k in ('layerId','boundaryYearRepresented','geometryResolution')):
                raise ValueError('Administrative boundary provenance missing: '+row['id']+' / '+name)
            assigned=row.get('admin1Code' if name=='ADM1' else 'admin2Code')
            if level.get('status')!='matched' and assigned:raise ValueError('Unresolved administrative level asserts an ID')
    reports=row.get('reports')
    if not isinstance(reports,list) or not reports:raise ValueError('Incident has no source evidence')
    report_ids=set();report_sources=set()
    for report in reports:
        if report.get('incidentId')!=row['id'] or report.get('id') in report_ids or not report.get('id'):raise ValueError('Report identity conflict')
        report_ids.add(report['id']);sid=report.get('sourceId');source=sources.get(sid)
        if not source or row['countryCode'] not in source['countryCodes']:raise ValueError('Evidence source lacks country scope')
        if not pipeline.safe_url(report.get('url','')):raise ValueError('Unsafe evidence URL')
        undated_ucdp=(row['historical'] and row.get('sourceMetadata',{}).get('publicationTimeUnknown') is True
                      and all(sources.get(s,{}).get('kind')=='ucdp' for s in row.get('sourceIds',[])))
        if not (undated_ucdp and report.get('publishedAt')==''):timestamp(report['publishedAt'])
        timestamp(report['retrievedAt'])
        for key in ('title','language','contentHash','independentGroup'):
            if not isinstance(report.get(key),str) or not report[key]:raise ValueError('Missing report '+key)
        if not isinstance(report.get('excerpt'),str):raise ValueError('Missing report excerpt')
        report_sources.add(sid)
    if set(row.get('sourceIds',[]))!=report_sources or len(row.get('sourceIds',[]))!=len(report_sources):raise ValueError('Incident/source evidence sets differ')
    canonical(row)
    if len(pipeline.encode(row))+3>MAX_PART:raise ValueError('Complete incident exceeds12MB; cannot truncate')

def event_keys(row):
    meta=row.get('sourceMetadata',{});keys=[]
    for key in ('eventKey','outbreakKey','providerEventKey'):
        if isinstance(meta.get(key),(str,int)):
            keys.append((tuple(sorted(row['sourceIds'])),row['countryCode'],row['category'],key,str(meta[key])))
    # These are stable IDs supplied by named structured providers, not NLP guesses.
    if 'recordId' in meta:
        keys.append((tuple(sorted(row['sourceIds'])),row['countryCode'],row['category'],'recordId',str(meta['recordId'])))
    if 'eventId' in meta and 'eventType' in meta:
        keys.append((tuple(sorted(row['sourceIds'])),row['countryCode'],row['category'],'event',str(meta['eventType'])+':'+str(meta['eventId'])))
    return keys

def merge_record(old,new,now,reason):
    if old is None:return copy.deepcopy(new)
    if old['countryCode']!=new['countryCode']:raise ValueError('Same incident identity changed country')
    if timestamp(new['updatedAt'])<timestamp(old['updatedAt']):
        result=copy.deepcopy(old)
        signature=digest({k:v for k,v in new.items() if k not in ('firstSeenAt','historical','revisions')})
        represented=result.setdefault('sourceMetadata',{}).setdefault('olderImportedRepresentations',[])
        if signature in represented:return result
        prior_reports=[]
        for report in new['reports']:
            item=copy.deepcopy(report);item['incidentId']=old['id'];prior_reports.append(item)
        if all(any(r['url']==item['url'] and r['contentHash']==item['contentHash'] for r in old['reports']) for item in prior_reports):
            # Identical evidence needs no revision merely because an archive was checked.
            if not represented:result['sourceMetadata'].pop('olderImportedRepresentations',None)
            return result
        represented.append(signature)
        result.setdefault('revisions',[]).append(dict(at=now,note='Older provider representation retained as evidence; newer existing incident fields were preserved.',previousReports=prior_reports))
        return result
    result=copy.deepcopy(new);result['id']=old['id'];result['firstSeenAt']=min(old['firstSeenAt'],new['firstSeenAt'],key=timestamp);result['updatedAt']=max(old['updatedAt'],new['updatedAt'],key=timestamp)
    # Keep distinct source reports; a changed version of the same report is held in
    # the revision rather than being counted as independent corroboration.
    reports={r['id']:copy.deepcopy(r) for r in old['reports']}
    by_url={(r['sourceId'],r['url']):r['id'] for r in old['reports']}
    for report in new['reports']:
        item=copy.deepcopy(report);item['incidentId']=old['id']
        prior_id=by_url.get((item['sourceId'],item['url']))
        if prior_id:item['id']=prior_id
        reports[item['id']]=item
    result['reports']=list(reports.values());result['sourceIds']=sorted({r['sourceId'] for r in result['reports']})
    result['sourceMetadata']={**old.get('sourceMetadata',{}),**new.get('sourceMetadata',{})}
    result['revisions']=copy.deepcopy(old.get('revisions',[]))
    for revision in new.get('revisions',[]):
        if digest(revision) not in {digest(r) for r in result['revisions']}:result['revisions'].append(copy.deepcopy(revision))
    previous={k:v for k,v in old.items() if k not in ('reports','revisions','sourceMetadata','firstSeenAt','updatedAt') and new.get(k)!=v}
    check_old={k:v for k,v in old.items() if k!='revisions'};check_new={k:v for k,v in result.items() if k!='revisions'}
    if check_old!=check_new:
        result['revisions'].append(dict(at=now,note=reason,previousReports=copy.deepcopy(old['reports'])))
        if previous:
            history=copy.deepcopy(old.get('sourceMetadata',{}).get('fieldRevisions',[]))
            history.append(dict(at=now,note=reason,previousFields=previous))
            result['sourceMetadata']['fieldRevisions']=history
    if not result['revisions']:result.pop('revisions',None)
    return result

def project_current(records,countries):
    # Canonical helper is shared with scheduled publication, not an importer-only
    # approximation. Falls back only for a pre-projection pipeline in offline tests.
    if hasattr(pipeline,'current_snapshot_rows'):return pipeline.current_snapshot_rows(records,countries)
    rows=sorted([r for r in records.values() if not r['historical'] and r['countryCode'] in countries],key=lambda r:r['occurredAt'],reverse=True)
    return copy.deepcopy(rows)

def apply_correction(old,correction,now_text):
    patch=copy.deepcopy(old)
    fields=correction.get('patch',{})
    for key,value in fields.items():
        patch[key]={**patch.get(key,{}),**value} if key=='sourceMetadata' else copy.deepcopy(value)
    for key in correction.get('removeFields',[]):patch.pop(key,None)
    reports={r['id']:copy.deepcopy(r) for r in old['reports']}
    by_url={(r['sourceId'],r['url']):r['id'] for r in old['reports']}
    for report in correction.get('appendReports',[]):
        item=copy.deepcopy(report);item['incidentId']=old['id']
        item['id']=by_url.get((item['sourceId'],item['url']),item['id'])
        reports[item['id']]=item
    patch['reports']=list(reports.values());patch['sourceIds']=sorted({r['sourceId'] for r in patch['reports']})
    if patch==old:return copy.deepcopy(old)
    provider_time=fields.get('updatedAt')
    if not provider_time:patch['updatedAt']=now_text
    result=merge_record(old,patch,now_text,correction['reason'])
    if provider_time and timestamp(provider_time)<timestamp(old['updatedAt']):
        # Older catalog facts cannot overwrite a newer feed. Two independent
        # precision corrections remain valid: a declared GDACS centroid must not
        # be presented as an impact point, and unchanged points can be checked
        # against the current administrative hierarchy.
        meta=fields.get('sourceMetadata',{})
        demote=('gdacs' in old['sourceIds'] and fields.get('location')=={'precision':'country'}
                and meta.get('locationMeaning')=='provider_centroid_not_impact_location')
        same_point=(old.get('location',{}).get('precision')=='point'
                    and old.get('location')==fields.get('location') and 'adminAssignment' in fields)
        if demote or same_point:
            geographic=copy.deepcopy(result)
            keys={'admin1Id','admin1Code','admin1Name','admin2Id','admin2Code','admin2Name','adminAssignment'}
            if demote:keys.add('location')
            for key in keys:
                if key in fields:geographic[key]=copy.deepcopy(fields[key])
                elif key in correction.get('removeFields',[]):geographic.pop(key,None)
            if demote:
                geographic.setdefault('sourceMetadata',{}).update({k:copy.deepcopy(meta[k]) for k in ('providerClass','providerPointLabel','locationMeaning') if k in meta})
            if geographic!=result:
                geographic['updatedAt']=now_text
                result=merge_record(result,geographic,now_text,'Corrected geographic precision/hierarchy while retaining newer provider facts and dates.')
    return result

def prepare(root,batch_paths,now):
    now_text=pipeline.stamp(now);data=root/'docs/data'
    all_countries={r['code']:r for r in pipeline.load(root/'config/countries.json')}
    active={k:v for k,v in all_countries.items() if v['status']=='active' and v['reviewDue']>=now.date().isoformat()}
    sources={r['id']:r for r in pipeline.load(root/'config/sources.json')}
    areas={r['id']:r for r in pipeline.load(root/'config/admin-places.json')}
    records=pipeline.read_archive(data,all_countries);original_count=len(records)
    original_ids=set(records);changes=[];aliases={};batch_proofs=[];coverage=[];history_notes=[]
    discovered={r['id']:r for r in pipeline.load(data/'discovery.json',[])}
    batches=[pipeline.load(path) for path in batch_paths]
    # Admit all source rows first so corrections may cite evidence in another batch.
    for path,batch in zip(batch_paths,batches):
        batch_proofs.append(dict(file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),incoming=len(batch.get('incidents',[]))))
        for source in batch.get('newSources',[]):
            sid=source['id']
            if not pipeline.safe_url(source['url']) or not source['countryCodes'] or any(c not in all_countries for c in source['countryCodes']):raise ValueError('Invalid batch source scope/URL')
            old=sources.get(sid)
            if old and old['url']!=source['url']:raise ValueError('Existing source identity changed')
            merged_source={**(old or {}),**source}
            if not merged_source.get('verifiedAt'):raise ValueError('Batch source needs a verifiedAt review timestamp')
            if timestamp(merged_source['verifiedAt'])>now+dt.timedelta(minutes=5):raise ValueError('Batch source review timestamp is in the future')
            sources[sid]=merged_source
    equivalence={}
    for iid,row in records.items():
        for key in event_keys(row):equivalence.setdefault(key,set()).add(iid)
    for batch in batches:
        for incoming in batch.get('incidents',[]):
            row=copy.deepcopy(incoming);explicit=row.pop('mergeInto',None)
            row['historical']=timestamp(row['occurredAt'])<now-dt.timedelta(days=30)
            if row['historical'] and row['state']=='reported':row['state']='expired'
            validate_record(row,active,sources,areas,now)
            matches=set().union(*(equivalence.get(k,set()) for k in event_keys(row))) if event_keys(row) else set()
            if explicit:matches.add(explicit)
            if row['id'] in records:matches.add(row['id'])
            if len(matches)>1:raise ValueError('Conflicting exact source-event equivalence: '+row['id'])
            target=next(iter(matches),row['id'])
            if explicit and target not in records:raise ValueError('Explicit merge target missing')
            if target!=row['id']:aliases[row['id']]=target
            merged=merge_record(records.get(target),row,now_text,'Imported complete validated source representation; earlier evidence retained.')
            if records.get(target)!=merged:changes.append(target)
            records[target]=merged
            for key in event_keys(merged):equivalence.setdefault(key,set()).add(target)
        for correction in batch.get('corrections',[]):
            iid=correction['incidentId'];old=records.get(iid)
            if old is None:raise ValueError('Correction target missing: '+iid)
            changed=apply_correction(old,correction,now_text)
            if changed!=old:changes.append(iid)
            records[iid]=changed
        for entry in batch.get('coverage',[]) if isinstance(batch.get('coverage'),list) else []:coverage.append(entry)
        for note in batch.get('historyNotes',[]):
            if not isinstance(note,str) or not note:raise ValueError('Invalid history coverage note')
            history_notes.append(note)
        for entry in batch.get('discoveries',[]):discovered[entry['id']]=entry
    for row in records.values():
        row['historical']=timestamp(row['occurredAt'])<now-dt.timedelta(days=30)
        if row['historical'] and row['state']=='reported':row['state']='expired'
    # Validate all newly changed records, preserving inactive archive rows internally.
    for iid in set(changes):validate_record(records[iid],active,sources,areas,now)
    snapshot=pipeline.load(data/'snapshot.json');snapshot.pop('publication',None)
    snapshot.update(generatedAt=now_text,mode='live',schemaVersion=1,countries=list(active.values()),sources=list(sources.values()),
                    incidents=project_current(records,active),archiveTotal=sum(r['countryCode'] in active and pipeline.public_incident(r) for r in records.values()))
    snapshot['history']['recordCount']=snapshot['archiveTotal'];snapshot['history']['to']=now_text
    snapshot['history']['notes']=list(dict.fromkeys(snapshot['history'].get('notes',[])+history_notes))
    current_coverage={c['sourceId']:c for c in snapshot['coverage']}
    for entry in coverage:current_coverage[entry['sourceId']]=entry
    for sid in sources:
        if sid not in current_coverage:current_coverage[sid]=dict(sourceId=sid,lastAttemptAt=None,lastSuccessAt=None,status='pending',message='Source metadata imported; no successful scheduled retrieval is asserted by this import.')
    snapshot['coverage']=list(current_coverage.values())
    snapshot['collector']['discoveryCount']=len(discovered)
    snapshot['collector']['incidentScope']='severe-cap-weather-v1'
    snapshot['history']['sources']=sorted(set(snapshot['history']['sources'])|{sid for row in records.values() for sid in row['sourceIds']})
    snapshot['publication']=dict(format='peace-corps-public-data-v1',generatedAt=now_text,revision=pipeline.publication_revision(snapshot),activeCountryCodes=sorted(active))
    if len(pipeline.encode(snapshot))>MAX_SNAPSHOT:raise ValueError('Projected snapshot exceeds10MB; full text may not be truncated')
    audit=dict(importedAt=now_text,batches=batch_proofs,previousArchiveCount=original_count,archiveCount=len(records),
        newIncidents=len(set(records)-original_ids),changedIncidents=len(set(changes)),aliases=aliases,
        currentCount=len(snapshot['incidents']),snapshotBytes=len(pipeline.encode(snapshot)),sourceCount=len(sources),
        publicArchiveCount=snapshot['archiveTotal'],allAcceptedRecordsComplete=True)
    return records,all_countries,active,sources,snapshot,list(discovered.values()),audit

def run(root,batch_paths,now,apply=False):
    records,countries,active,sources,snapshot,discoveries,audit=prepare(root,batch_paths,now)
    if not apply:return dict(audit,applied=False)
    # Stage and validate every complete output before replacing the local data tree.
    temp=Path(tempfile.mkdtemp(prefix='public-batch-import-'));staged=temp/'data'
    shutil.copytree(root/'docs/data',staged)
    try:
        pipeline.write_archive(staged,records,countries,active)
        pipeline.save(staged/'snapshot.json',snapshot);pipeline.save(staged/'discovery.json',discoveries)
        pipeline.save(staged/'batch-import-status.json',audit)
        reread=pipeline.read_archive(staged,countries)
        if reread!=records:raise ValueError('Complete archive roundtrip changed accepted records')
        for path in (staged/'history').glob('*.json'):
            if path.stat().st_size>MAX_PART:raise ValueError('Country archive part exceeds12MB')
        if (staged/'history.json').stat().st_size>25_000_000:raise ValueError('History index exceeds25MB reader ceiling')
        sources_path=root/'config/sources.json';old_sources=sources_path.read_bytes()
        backup=temp/'previous-data';live=root/'docs/data';os.replace(live,backup)
        try:
            os.replace(staged,live);pipeline.save(sources_path,list(sources.values()))
        except Exception:
            if live.exists():shutil.rmtree(live)
            os.replace(backup,live);sources_path.write_bytes(old_sources);raise
        return dict(audit,applied=True)
    finally:shutil.rmtree(temp,ignore_errors=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--batch',type=Path,action='append',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();print(json.dumps(run(args.root.resolve(),[p.resolve() for p in args.batch],dt.datetime.now(dt.timezone.utc),args.apply)))
