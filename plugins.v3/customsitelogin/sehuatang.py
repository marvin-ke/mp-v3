"""Site-specific identity and reward evidence; no independent MP plugin."""
import re
from datetime import datetime
from urllib.parse import urlsplit, parse_qs
from lxml import html

ORIGIN = 'https://sehuatang.org/'
PATHS = ('forum.php', 'home.php?mod=spacecp&ac=credit&op=base',
         'home.php?mod=spacecp&ac=credit&op=rule',
         'home.php?mod=spacecp&ac=credit&op=log&suboperation=creditrulelog')

def origin_for_cookies(cookies):
    for host in ('sehuatang.org', 'www.sehuatang.org'):
        domains = {host, '.' + host, '.sehuatang.org'}
        names = {cookie['name'] for cookie in cookies if cookie['domain'].lower() in domains}
        if any(name.endswith('_auth') and name[:-5] + '_saltkey' in names for name in names):
            return 'https://' + host + '/'
    return ORIGIN


def response_error(status, body):
    """Classify a remote error without retaining HTML, stack arguments, IPs or cookies."""
    if status!=503 or len(body)>2*1024*1024:return None
    try:
        document=html.fromstring(body.decode('utf-8','replace'))
        for node in document.xpath('//script|//style'):node.drop_tree()
        text=clean(document).casefold()
        if 'system error' in text and 'read error on connection to' in text and 'redis' in text:
            return 'site_redis_read_error'
    except Exception:pass
    return None


def clean(node):
    return ' '.join(node.text_content().split())


def parse_page(body):
    document = html.fromstring(body.decode('utf-8', 'replace'))
    script_uids = set(re.findall(r'\bdiscuz_uid\s*=\s*[\'\"]?(\d+)', body.decode('utf-8', 'replace')))
    for node in document.xpath('//script|//style'):
        node.drop_tree()
    logout = bool(document.xpath('//a[contains(@href,"action=logout")]'))
    uid = next(iter(script_uids)) if len(script_uids) == 1 else None
    links = document.xpath('//*[@id="um"]//a[@href]')
    own_link = False
    for link in links:
        parsed = urlsplit(link.get('href'))
        query = parse_qs(parsed.query)
        if parsed.hostname not in (None, 'sehuatang.org', 'www.sehuatang.org'):
            continue
        if uid and query.get('mod') == ['space'] and query.get('uid') == [uid]:
            own_link = True
    text = clean(document)
    logged = bool(logout and uid and uid != '0' and own_link and '您需要先登录' not in text)
    result = {'logged_in': logged, 'uid': uid if logged else None, 'total': None,
              'money': None, 'reward_time': None, 'reward_amount': None, 'reward_count': None,
              'rule_daily': False, 'rule_amount': None, 'rule_max': None}
    if not logged:
        return result
    for node in document.xpath('//a[contains(@href,"ac=credit")]'):
        match = re.fullmatch(r'积分\s*[:：]\s*([\d,]+)', clean(node))
        if match:
            result['total'] = int(match[1].replace(',', ''))
            break
    area = document.xpath('//*[@id="ct"]')
    area = area[0] if area else document
    match = re.search(r'金钱\s*[:：]\s*([+-]?[\d,]+)', clean(area))
    if match:
        result['money'] = int(match[1].replace(',', ''))
    for table in area.xpath('.//table'):
        rows = [[clean(c) for c in row.xpath('./th|./td')] for row in table.xpath('.//tr')]
        if not rows or len(rows[0]) != len(set(rows[0])):
            continue
        headers = rows[0]
        for cells in rows[1:]:
            if len(cells) != len(headers):
                continue
            row = dict(zip(headers, cells))
            if row.get('动作名称') != '每天登录':
                continue
            if {'周期范围', '周期内最多奖励次数', '金钱'} <= row.keys():
                if re.fullmatch(r'[+-]?\d+', row['金钱']) and row['周期内最多奖励次数'].isdigit():
                    result.update(rule_daily=row['周期范围'] == '每天', rule_amount=int(row['金钱']),
                                  rule_max=int(row['周期内最多奖励次数']))
            if {'总次数', '周期次数', '金钱', '最后奖励时间'} <= row.keys():
                if re.fullmatch(r'[+-]?\d+', row['金钱']) and row['周期次数'].isdigit():
                    datetime.strptime(row['最后奖励时间'], '%Y-%m-%d %H:%M')
                    result.update(reward_time=row['最后奖励时间'], reward_amount=int(row['金钱']),
                                  reward_count=int(row['周期次数']))
    return result



def evaluate(pages, ended):
    from .core import stamp
    forum, base, rule, system = pages
    if base['money'] is None:
        raise ValueError('credit fields missing')
    when = stamp(system['reward_time']) if system['reward_time'] else None
    confirmed = bool(when and when.date() == ended.date() and when <= ended
                     and rule['rule_daily'] and rule['rule_max'] == 1 and rule['rule_amount'] == 1
                     and system['reward_amount'] == 1 and system['reward_count'] == 1)
    return {'total': base['total'], 'money': base['money'], 'reward_time': system['reward_time'],
            'reward_amount': system['reward_amount'], 'reward_confirmed': confirmed,
            'reward_evidence': '每日规则及系统奖励记录' if confirmed else None}
