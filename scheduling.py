"""Half-open entry windows evaluated in one explicit IANA timezone."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import uuid

UTC=timezone.utc


def get_zone(name):
    try:return ZoneInfo(name)
    except (ZoneInfoNotFoundError,TypeError,ValueError):raise ValueError('Choose a valid timezone, such as Europe/London') from None


def local_instant(value, zone):
    if not isinstance(value,str):raise ValueError('Choose a start and end time')
    try:date=datetime.fromisoformat(value)
    except ValueError:raise ValueError('Choose a valid date and time') from None
    if date.tzinfo is not None:return date.timestamp()
    first=date.replace(tzinfo=zone,fold=0)
    second=date.replace(tzinfo=zone,fold=1)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)!=date:
        raise ValueError('That time does not exist because the clocks change. Choose another time.')
    if first.utcoffset()!=second.utcoffset():
        raise ValueError('That time occurs twice because the clocks change. Choose an unambiguous time.')
    return first.timestamp()


def clock(value):
    try:
        hour,minute=map(int,value.split(':'))
        if len(value)!=5 or not 0<=hour<=23 or not 0<=minute<=59:raise ValueError()
        return hour*60+minute
    except (AttributeError,ValueError):raise ValueError('Use a valid clock time, such as 09:00') from None


def validate_window(body, zone_name, now):
    if not isinstance(body,dict):raise ValueError('Invalid time window')
    zone=get_zone(zone_name)
    enabled=body.get('enabled',True)
    if type(enabled) is not bool:raise ValueError('Invalid enabled setting')
    window={'id':uuid.uuid4().hex,'kind':body.get('kind'),'enabled':enabled}
    if window['kind']=='once':
        start=local_instant(body.get('start'),zone);end=local_instant(body.get('end'),zone)
        if end<=start:raise ValueError('The end must be after the start')
        if end<=now:raise ValueError('This window has already ended')
        if end-start>31*86400:raise ValueError('A one-off window can last up to 31 days')
        window.update(start=start,end=end)
    elif window['kind']=='weekly':
        days=body.get('days')
        if not isinstance(days,list) or not days or any(type(day)is not int or day not in range(7) for day in days):
            raise ValueError('Choose at least one day')
        start=clock(body.get('start'));end=clock(body.get('end'))
        if start==end:raise ValueError('Choose different start and end times')
        window.update(days=sorted(set(days)),start=body['start'],end=body['end'])
    else:raise ValueError('Choose a one-off or weekly window')
    return window


def intervals(settings,now):
    """Return relevant UTC intervals, including overnight weekly windows."""
    result=[]
    zone=get_zone(settings.get('timezone','Europe/London'))
    today=datetime.fromtimestamp(now,zone).date()
    for window in settings.get('windows',[]):
        if not window.get('enabled',True):continue
        if window['kind']=='once':result.append((window['start'],window['end']))
        else:
            start=clock(window['start']);end=clock(window['end'])
            for offset in range(-1,9):
                day=today+timedelta(days=offset)
                if day.weekday() not in window['days']:continue
                midnight=datetime.combine(day,datetime.min.time())
                begin=(midnight+timedelta(minutes=start)).replace(tzinfo=zone).timestamp()
                finish=(midnight+timedelta(days=1 if end<start else 0,minutes=end)).replace(tzinfo=zone).timestamp()
                if finish>begin:result.append((begin,finish))
    manual=settings.get('armed_until',0)
    if settings.get('unlock',False) and manual>now:result.append((now,manual))
    return result


def entry_state(settings,now):
    zone=settings.get('timezone','Europe/London')
    paused=settings.get('paused',False)
    ranges=intervals(settings,now)
    active=not paused and any(start<=now<end for start,end in ranges)
    # Collapse overlapping/adjacent windows to give a truthful next close time.
    merged=[]
    for start,end in sorted(ranges):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    until=next((end for start,end in merged if start<=now<end),None) if active else None
    next_start=next((start for start,end in merged if start>now),None)
    return {'active':active,'paused':paused,'until':until,'next_start':next_start,
            'timezone':zone,'manual_until':settings.get('armed_until',0)}
