"""Shared CookieCloud selection, fixed-site HTTP checks and Beijing-time records."""
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from . import chiphell, sehuatang, dian115
TZ = ZoneInfo('Asia/Shanghai')
SITES = {
    'dian115': {'name': '颠影 MADREEL', 'adapter': dian115, 'domains': dian115.DOMAINS,
                'balance_field': 'points', 'currency': '积分', 'cron': '17 9,21 * * *', 'proxy': True,
                'legacy': None},
    'chiphell': {'name': 'Chiphell', 'adapter': chiphell, 'domains': ('chiphell.com', 'www.chiphell.com'),
                 'balance_field': 'evil', 'currency': '邪恶指数', 'cron': '37 9,21 * * *', 'proxy': False,
                 'legacy': 'ChiphellAutoLogin'},
    'sehuatang': {'name': '色花堂', 'adapter': sehuatang, 'domains': ('sehuatang.org', 'www.sehuatang.org'),
                  'balance_field': 'money', 'currency': '金钱', 'cron': '43 9,21 * * *', 'proxy': True,
                  'legacy': 'SehuatangAutoLogin'},
}

def stamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def load_candidates(specs, cookie_root, decrypt, now, domains):
    candidates, errors = [], []
    for spec in specs:
        label = spec['label']
        try:
            key = spec['key']
            if not re.fullmatch(r'[a-zA-Z0-9]{5,128}', key):
                raise ValueError('invalid key')
            path = cookie_root / (key + '.json')
            container = json.loads(path.read_text())
            data = decrypt(container['encrypted'], key, spec['password'])
            when = stamp(data['update_time']) if data.get('update_time') else datetime.fromtimestamp(path.stat().st_mtime, TZ)
            if when > now + timedelta(minutes=10):
                raise ValueError('future timestamp')
            cookies = {}
            for items in data.get('cookie_data', {}).values():
                if not isinstance(items, list):
                    continue
                for c in items:
                    domain = (c.get('domain') or '').lstrip('.').lower()
                    if domain not in domains:
                        continue
                    if not c.get('name') or not c.get('value'):
                        continue
                    if c.get('expirationDate') and float(c['expirationDate']) < now.timestamp():
                        continue
                    cookies[(c['name'], c['domain'], c.get('path') or '/')] = dict(c)
            names = {c['name'] for c in cookies.values()}
            complete = ('portal_token' in names if 'm.dian115.com' in domains else
                        any(n.endswith('_auth') and n[:-5] + '_saltkey' in names for n in names))
            if not complete:
                raise ValueError('missing complete login cookies')
            candidates.append({'label': label, 'updated_at': when.isoformat(), 'cookies': list(cookies.values())})
        except Exception as exc:
            errors.append({'source': label, 'reason': type(exc).__name__})
    candidates.sort(key=lambda c: stamp(c['updated_at']), reverse=True)
    return candidates, errors



def display_time(value):
    if value is None or value == '':
        return None
    try:
        return stamp(value).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def normalise(record):
    if not isinstance(record, dict):
        return None
    result = dict(record)
    for key in ('checked_at', 'source_update', 'reward_time'):
        if key in result:
            result[key] = display_time(result[key])
    return result

def done_today(record, now, expected_uid):
    return bool(record and record.get('reward_confirmed') and record.get('day') == now.date().isoformat()
                and record.get('uid') == str(expected_uid))


class _Cancelled(RuntimeError):
    pass


class _AccountRejected(RuntimeError):
    pass

class _ResponseRejected(RuntimeError):
    """A response already recorded using a sanitized HTTP/category code."""
    pass


def run_check(site_id, candidates, expected_uid, now, previous=None, session_factory=requests.Session,
              should_stop=None, clock=None, proxies=None):
    adapter = SITES[site_id]["adapter"]
    paths = adapter.PATHS
    attempts, partial = [], None

    def fresh(instant=None):
        instant = instant if instant is not None else (clock() if clock else now)
        return {'day': instant.date().isoformat(), 'checked_at': instant.isoformat(),
                'logged_in': False, 'reward_confirmed': False, 'reward_check_complete':False, 'status': 'failed', 'attempts': attempts}

    def check_running():
        if should_stop and should_stop():
            raise _Cancelled()

    def balance_change(value, field=None):
        old = previous or {}
        field = field or SITES[site_id]['balance_field']
        if old.get('uid') not in (None, str(expected_uid)):
            return None
        return value - old[field] if isinstance(value, int) and isinstance(old.get(field), int) else None

    for candidate in candidates:
        origin = adapter.ORIGIN
        if site_id == 'sehuatang':
            origin = adapter.origin_for_cookies(candidate['cookies'])
        verified = {}
        try:
            check_running()
            with session_factory() as session:
                session.trust_env = False
                session.proxies.update(proxies or {})
                session.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
                                        'Accept-Language': 'zh-CN,zh;q=0.9', 'Referer': origin})
                for c in candidate['cookies']:
                    session.cookies.set(c['name'], c['value'], domain=c['domain'], path=c.get('path') or '/', secure=bool(c.get('secure')))
                pages = []
                for path in paths:
                    check_running()
                    response = session.get(origin + path, timeout=(8, 20), allow_redirects=False)
                    check_running()
                    if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
                        if site_id=='sehuatang':
                            failure={'source':candidate['label'],'status':'HTTP_'+str(response.status_code) if response.status_code!=200 else 'ResponseTooLarge'}
                            kind=adapter.response_error(response.status_code,response.content)
                            if kind:failure['response_error']=kind
                            attempts.append(failure)
                            raise _ResponseRejected()
                        if site_id=='chiphell':attempts.append({'source':candidate['label'],'status':'HTTP_'+str(response.status_code) if response.status_code!=200 else 'ResponseTooLarge'})
                        raise ValueError('invalid HTTP response')
                    parsed = adapter.parse_page(response.content)
                    if not parsed['logged_in'] or parsed['uid'] != str(expected_uid):
                        raise _AccountRejected()
                    verified.update(logged_in=True, uid=str(expected_uid), source_label=candidate['label'],
                                    source_update=candidate['updated_at'], status='unconfirmed')
                    for field in ('total', SITES[site_id]['balance_field']):
                        if parsed[field] is not None:
                            verified[field] = parsed[field]
                    pages.append(parsed)
                ended = clock() if clock else now
                evidence = adapter.evaluate(pages, ended)
                check_running()
                result = fresh(ended)
                result.update(verified, **evidence)
                result['reward_check_complete']=True
                field = SITES[site_id]['balance_field']
                result['balance_change'] = balance_change(result.get(field))
                result['total_change'] = balance_change(result.get('total'), 'total')
                if site_id == 'sehuatang':
                    result['money_change'] = result['balance_change']
                result['status'] = 'confirmed' if result['reward_confirmed'] else 'unconfirmed'
                attempts.append({'source': candidate['label'], 'status': result['status']})
                return result
        except _Cancelled:
            return dict(fresh(), status='cancelled')
        except _AccountRejected:
            verified = {}
            attempts.append({'source': candidate['label'], 'status': 'account_not_verified'})
        except _ResponseRejected:
            pass
        except Exception as exc:
            attempts.append({'source': candidate['label'], 'status': type(exc).__name__})
        if verified and partial is None:
            partial = verified
    result = fresh()
    if partial:
        result.update(partial, balance_change=balance_change(partial.get(SITES[site_id]['balance_field'])),
                      total_change=balance_change(partial.get('total'), 'total'))
        if site_id == 'sehuatang':
            result['money_change'] = result['balance_change']
    return result


def daily_history(history, site_id, expected_uid):
    """Daily closing observations; gaps/failed checks never become zero balances."""
    groups = {}
    for raw in history or []:
        if not isinstance(raw, dict):
            continue
        try:
            when = stamp(raw.get('checked_at'))
        except (ValueError, TypeError):
            continue
        groups.setdefault(when.date().isoformat(), []).append((when, normalise(raw)))
    previous = None
    previous_total = None
    daily = []
    field = SITES[site_id]['balance_field']
    is_number = lambda value: isinstance(value, int) and not isinstance(value, bool)
    for day in sorted(groups):
        entries = sorted(groups[day], key=lambda pair: pair[0])
        latest = entries[-1][1]
        valid = [row for _, row in entries if row.get('logged_in') and row.get('uid') == str(expected_uid)]
        # A later failed/incomplete check does not erase an earlier measured balance.
        balances = [row for row in valid if is_number(row.get(field))]
        observed = balances[-1] if balances else {}
        totals = [row for row in valid if is_number(row.get('total'))]
        total_observed = totals[-1] if totals else {}
        confirmed = [row for row in valid if row.get('reward_confirmed') and row.get('day') == day]
        balance = observed.get(field)
        total = total_observed.get('total')
        old = previous or {}
        balance_change = balance - old['balance'] if is_number(balance) and is_number(old.get('balance')) else None
        old_total = previous_total or {}
        total_change = total - old_total['total'] if is_number(total) and is_number(old_total.get('total')) else None
        latest_failed = not (latest.get('logged_in') and latest.get('uid') == str(expected_uid))
        row = {'day': day, 'logged_in': bool(valid), 'latest_failed': latest_failed,
               'total': total, 'balance': balance, 'total_change': total_change,
               'balance_change': balance_change, 'previous_observed_day': old.get('day') if is_number(balance) else None,
               'previous_total_observed_day': old_total.get('day') if is_number(total) else None,
               'reward_status': ('已确认签到与转盘' if site_id == 'dian115' else '已确认 +1') if confirmed else '未确认',
               'reward_time': confirmed[-1].get('reward_time') if confirmed else None,
               'attempts_count': len(entries), 'checked_at': latest.get('checked_at'),
               'observed_at': observed.get('checked_at'), 'latest': latest}
        daily.append(row)
        if is_number(balance):
            previous = {'day': day, 'balance': balance}
        if is_number(total):
            previous_total = {'day': day, 'total': total}
    return list(reversed(daily))
