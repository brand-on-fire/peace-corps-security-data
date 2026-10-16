"""Bounded WordPress archive pagination; no incident inference or persistence.

The caller must commit the returned checkpoint only after accepting the complete
record transaction. A failed page never modifies its input checkpoint. One call
reads at most the previous anchor page and one new page.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import re
import urllib.parse

from local_news_archive import parse_wordpress
from public_news_runtime import PARSER, PROFILES

UTC = dt.timezone.utc
PAGE_SIZE = 100
MAX_PAGES = 500
MAX_POSTS = PAGE_SIZE * MAX_PAGES
REVIEWED_SOURCES = frozenset(('gov-jm-jcf', 'news-bz-breaking-belize-news',
    'news-zm-lusaka-times', 'news-gy-demerara-waves', 'gov-ke-kenya-broadcasting-corporation'))


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise ValueError('Archive timestamp must be a complete UTC second')
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configuration(source, policy, now):
    """Only the five already reviewed anonymous full-content profiles qualify."""
    if not isinstance(source, dict) or not isinstance(policy, dict):
        raise ValueError('Archive source and policy must be reviewed objects')
    window = policy.get('wordpressHistory')
    if not isinstance(window, dict) or set(window) != {'from', 'through'}:
        raise ValueError('WordPress history needs an explicit reviewed fixed window')
    start, end, current = timestamp(window['from']), timestamp(window['through']), timestamp(now)
    if not start < end <= current or end-start > dt.timedelta(days=366):
        raise ValueError('Archive window must be historical and no longer than one year')
    try:
        start-dt.timedelta(seconds=1)
        end+dt.timedelta(seconds=1)
    except OverflowError as error:
        raise ValueError('Archive window cannot represent its UTC boundary overlap') from error
    if any(not isinstance(source.get(key), str) for key in ('id','url','language','feedUrl')) or not isinstance(policy.get('url'),str):
        raise ValueError('Archive source identity fields must be strings')
    profile = PROFILES.get(source['id'])
    endpoint = urllib.parse.urlsplit(policy.get('url', ''))
    origin = urllib.parse.urlsplit(source.get('url', ''))
    if (source.get('id') not in REVIEWED_SOURCES or not profile or source.get('enabled') is not True or policy.get('sourceId') != source['id']
            or policy.get('newsAdapter') != PARSER or policy.get('format') != 'wordpress'
            or policy.get('mode') != 'discovery' or source.get('language', '').split('-')[0] != 'en'
            or source.get('countryCodes') != [profile['country']]
            or source.get('kind') != 'rss' or source.get('access') != 'public-feed'
            or source.get('tier') != ('official' if source['id'].startswith('gov-') else 'local-news')
            or source.get('feedUrl') != policy.get('url')
            or endpoint.scheme != 'https' or endpoint.hostname != profile['host']
            or origin.scheme != 'https' or origin.hostname != profile['host']
            or endpoint.username or endpoint.password or endpoint.port or endpoint.fragment
            or origin.username or origin.password or origin.port or origin.query or origin.fragment
            or origin.path not in ('', '/')
            or endpoint.path != '/wp-json/wp/v2/posts'
            or urllib.parse.parse_qs(endpoint.query, keep_blank_values=True) != {'per_page':['100'], 'orderby':['modified'], 'order':['desc']}):
        raise ValueError('Archive source identity or current-feed policy is not reviewed')
    return dict(sourceId=source['id'], parser=PARSER, countryCode=profile['country'],
                from_=window['from'], through=window['through'],
                endpoint=urllib.parse.urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, '', '')))


def empty_checkpoint(config, now, generation=1, restarts=0):
    return dict(schemaVersion=1, sourceId=config['sourceId'], configurationHash=digest(config),
                window={'from':config['from_'], 'through':config['through']},
                generation=generation, restarts=restarts, nextPage=1, totalPosts=None,
                totalPages=None, seenIds=[], previousPageIdentity=None, pagesAccepted=0,
                windowPosts=0, complete=False, status='pending', startedAt=now)


def checkpoint(config, previous, now):
    if previous is None:
        return empty_checkpoint(config, now)
    if not isinstance(previous, dict):
        raise ValueError('Archive checkpoint must be an object or absent')
    required = {'schemaVersion','sourceId','configurationHash','window','generation','restarts',
        'nextPage','totalPosts','totalPages','seenIds','previousPageIdentity','pagesAccepted',
        'windowPosts','complete','status','startedAt'}
    optional = {'restartReason','lastAttemptAt','lastPageAcceptedAt','completedAt'}
    if not required <= set(previous) or set(previous)-required-optional:
        raise ValueError('Archive checkpoint has missing or unknown fields')
    if 'restartReason' in previous and (not isinstance(previous['restartReason'],str) or len(previous['restartReason'])>256):
        raise ValueError('Archive checkpoint restart reason is invalid')
    state = previous
    if (type(state.get('schemaVersion')) is not int or state['schemaVersion'] != 1 or state.get('sourceId') != config['sourceId']
            or state.get('configurationHash') != digest(config)
            or state.get('window') != {'from':config['from_'], 'through':config['through']}):
        raise ValueError('Archive checkpoint belongs to another source or window')
    for field, low, high in [('generation',1,1_000_000), ('restarts',0,1_000_000),
                             ('nextPage',1,MAX_PAGES+1), ('pagesAccepted',0,MAX_PAGES),
                             ('windowPosts',0,MAX_POSTS)]:
        if type(state.get(field)) is not int or not low <= state[field] <= high:
            raise ValueError('Archive checkpoint has invalid progress counters')
    if state['generation'] != state['restarts']+1:
        raise ValueError('Archive checkpoint restart counters disagree')
    ids = state.get('seenIds')
    if (not isinstance(ids,list) or len(ids)>MAX_POSTS
            or any(not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',value) for value in ids)
            or len(set(ids))!=len(ids)
            or type(state.get('complete')) is not bool or state['windowPosts']>len(ids)
            or state['nextPage'] != state['pagesAccepted']+1):
        raise ValueError('Archive checkpoint has invalid article identities')
    if state['pagesAccepted'] == 0:
        if (ids or state.get('totalPosts') is not None or state.get('totalPages') is not None
                or state.get('previousPageIdentity') is not None or state['complete']
                or not all(key in state for key in ('totalPosts','totalPages','previousPageIdentity'))):
            raise ValueError('Empty archive checkpoint contains inconsistent progress')
    else:
        total, pages = pagination(state.get('totalPosts'),state.get('totalPages'))
        if not isinstance(state.get('previousPageIdentity'),str) or not re.fullmatch(r'[a-f0-9]{64}',state['previousPageIdentity']):
            raise ValueError('Archive checkpoint has no exact prior-page identity')
        expected = min(state['pagesAccepted']*PAGE_SIZE,total)
        if len(ids)!=expected or state['pagesAccepted']>max(pages,1) or state['complete'] != (state['pagesAccepted']==max(pages,1)):
            raise ValueError('Archive checkpoint totals do not match accepted pages')
    current = timestamp(now)
    started = timestamp(state.get('startedAt'))
    if not timestamp(config['through']) <= started <= current:
        raise ValueError('Archive checkpoint start is outside its observation window')
    expected_status = ('scanned' if state['complete'] else 'in-progress') if state['pagesAccepted'] else ('restart-required' if state['restarts'] else 'pending')
    if state.get('status') != expected_status:
        raise ValueError('Archive checkpoint status disagrees with progress')
    for key in ('lastAttemptAt','lastPageAcceptedAt','completedAt'):
        if key in state and not started <= timestamp(state[key]) <= current:
            raise ValueError('Archive checkpoint observation clocks are inconsistent')
    if state['pagesAccepted']:
        if state.get('lastAttemptAt') != state.get('lastPageAcceptedAt') or 'lastPageAcceptedAt' not in state:
            raise ValueError('Archive checkpoint lacks its accepted-page observation')
    elif 'lastPageAcceptedAt' in state or 'completedAt' in state:
        raise ValueError('Unscanned archive checkpoint contains completion clocks')
    if state['complete']:
        if state.get('completedAt') != state.get('lastPageAcceptedAt'):
            raise ValueError('Archive completion clock disagrees with accepted page')
    elif 'completedAt' in state:
        raise ValueError('Incomplete archive checkpoint has a completion clock')
    if state['restarts'] and not state['pagesAccepted'] and state.get('lastAttemptAt') != state['startedAt']:
        raise ValueError('Archive restart checkpoint lacks its restart observation')
    return copy.deepcopy(state)


def pagination(total, pages):
    if type(total) is not int or type(pages) is not int or not 0<=total<=MAX_POSTS or not 0<=pages<=MAX_PAGES:
        raise ValueError('Archive pagination totals are missing or outside the reviewed limit')
    if pages != math.ceil(total/PAGE_SIZE):
        raise ValueError('Archive pagination totals are inconsistent')
    return total,pages


def page_url(config, page):
    # One-second overlap makes the accepted [from,through) UTC interval explicit
    # regardless of whether a provider treats its boundary second inclusively.
    query = urllib.parse.urlencode(dict(after=stamp(timestamp(config['from_'])-dt.timedelta(seconds=1)),
        before=stamp(timestamp(config['through'])+dt.timedelta(seconds=1)),
        per_page=PAGE_SIZE,page=page,orderby='date',order='desc'))
    return config['endpoint']+'?'+query


def identity(items):
    return digest([(item['guid'],item['publishedAt']) for item in items])


def read_page(source, policy, config, page, now, fetch, fixtures):
    url = page_url(config,page)
    child = dict(policy, sourceId=source['id']+'-history-page-'+str(page),url=url)
    result,raw = fetch(child,{},now,fixtures)
    if (not isinstance(result,dict) or not isinstance(raw,bytes)
            or type(result.get('httpStatus')) is not int or result['httpStatus'] != 200
            or result.get('finalUrl') != url):
        raise ValueError('Archive page did not return its complete requested HTTP 200 representation')
    headers = result.get('wordpressPagination',{})
    if not isinstance(headers,dict):
        raise ValueError('Archive pagination totals are missing or malformed')
    total,pages = pagination(headers.get('total'),headers.get('pages'))
    if page>max(pages,1):
        raise ValueError('Archive page is outside the declared page range')
    items = parse_wordpress(raw,source)
    expected = min(PAGE_SIZE,max(0,total-(page-1)*PAGE_SIZE))
    if len(items)!=expected:
        raise ValueError('Complete archive page count does not match provider totals')
    if any(not re.fullmatch(r'[1-9][0-9]{0,19}', item['guid']) for item in items):
        raise ValueError('Archive article identities must be positive bounded integers')
    dates = [timestamp(item['publishedAt']) for item in items]
    if any(not published <= timestamp(item['modifiedAt']) <= timestamp(now) for published,item in zip(dates,items)):
        raise ValueError('Archive modification clocks are inconsistent')
    lower,upper = timestamp(config['from_'])-dt.timedelta(seconds=1),timestamp(config['through'])+dt.timedelta(seconds=1)
    if dates != sorted(dates,reverse=True) or any(not lower<=value<=upper for value in dates):
        raise ValueError('Archive ordering or UTC date filtering changed')
    receipt = dict(page=page,url=url,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),
                   totalPosts=total,totalPages=pages,items=len(items),identityHash=identity(items))
    return items,receipt


def restart(config, state, now, receipts, reason):
    if state['generation'] >= 1_000_000:
        raise ValueError('Archive restart bound reached; explicit review required')
    reset = empty_checkpoint(config,now,state['generation']+1,state['restarts']+1)
    reset.update(status='restart-required',lastAttemptAt=now,restartReason=reason)
    return dict(items=[],checkpoint=reset,receipts=receipts,requested=len(receipts))


def advance(source, policy, previous, now, fetch, fixtures=None):
    """Return at most two complete pages and an uncommitted next checkpoint.

    Counts and the overlapping anchor are checked on every resumed page. Changes
    restart the bounded scan; accepted incident history belongs to the caller and
    is never deleted. Counts alone cannot establish a transactional provider view.
    """
    config = configuration(source,policy,now)
    state = checkpoint(config,previous,now)
    if state['complete']:
        return dict(items=[],checkpoint=state,receipts=[],requested=0)
    page = state['nextPage'];receipts=[];anchor=[]
    if page>1:
        anchor,receipt = read_page(source,policy,config,page-1,now,fetch,fixtures)
        receipts.append(receipt)
        if (receipt['totalPosts']!=state['totalPosts'] or receipt['totalPages']!=state['totalPages']
                or receipt['identityHash']!=state['previousPageIdentity']):
            return restart(config,state,now,receipts,
                'Provider totals or overlapping page identities changed; accepted records retained.')
    items,receipt = read_page(source,policy,config,page,now,fetch,fixtures)
    receipts.append(receipt)
    if page>1 and (receipt['totalPosts'],receipt['totalPages']) != (state['totalPosts'],state['totalPages']):
        return restart(config,state,now,receipts,
            'Provider totals changed between adjacent pages; accepted records retained.')
    new_ids = [item['guid'] for item in items]
    if set(new_ids).intersection(state['seenIds']):
        return restart(config,state,now,receipts,
            'Archive pagination repeated an accepted identity; accepted records retained.')
    if anchor and items and timestamp(items[0]['publishedAt'])>timestamp(anchor[-1]['publishedAt']):
        raise ValueError('Archive chronology changed across page boundaries')
    within = lambda item: config['from_']<=item['publishedAt']<config['through']
    state.update(totalPosts=receipt['totalPosts'],totalPages=receipt['totalPages'],
                 nextPage=page+1,pagesAccepted=page,seenIds=state['seenIds']+new_ids,
                 previousPageIdentity=receipt['identityHash'],lastAttemptAt=now,lastPageAcceptedAt=now,
                 windowPosts=state['windowPosts']+sum(within(item) for item in items))
    state['complete'] = page==max(state['totalPages'],1)
    state['status'] = 'scanned' if state['complete'] else 'in-progress'
    if state['complete']:
        if len(state['seenIds'])!=state['totalPosts']:
            raise ValueError('Archive end reached without every declared identity')
        state['completedAt']=now
    # Reinspect anchor content for corrections even if its ID/date stayed fixed.
    # Full text was parsed before any explicit publication-window selection.
    return dict(items=[item for item in anchor+items if within(item)],checkpoint=state,
                receipts=receipts,requested=len(receipts))
