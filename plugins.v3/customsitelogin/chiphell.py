"""Site-specific identity and reward evidence; no independent MP plugin."""
import re
from datetime import datetime
from urllib.parse import urlsplit, parse_qs
from lxml import html

ORIGIN = 'https://www.chiphell.com/'
PATHS = ('forum.php', 'home.php?mod=spacecp&ac=credit&op=base',
         'home.php?mod=spacecp&ac=credit&op=log&suboperation=creditrulelog',
         'home.php?mod=spacecp&ac=credit&op=log')


def clean(node):
    return ' '.join(node.text_content().split())


def parse_page(body):
    document = html.fromstring(body.decode('utf-8', 'replace'))
    script_uids = set(re.findall(r'\bdiscuz_uid\s*=\s*[\'\"]?(\d+)', body.decode('utf-8', 'replace')))
    for node in document.xpath('//script|//style'):
        node.drop_tree()
    logout = bool(document.xpath('//a[contains(@href,"action=logout")]'))
    uid = next(iter(script_uids)) if len(script_uids) == 1 else None
    links = document.xpath('//a[contains(@href,"space-uid-")]')
    own_link = bool(uid and any(re.search(r'\bspace-uid-' + re.escape(uid) + r'\.html', a.get('href') or '') for a in links))
    text = clean(document)
    logged = bool(logout and uid and uid != '0' and own_link and '您需要先登录' not in text)
    result = {'logged_in': logged, 'uid': uid if logged else None, 'total': None,
              'evil': None, 'reward_time': None, 'reward_amount': None, 'ledger': []}
    if not logged:
        return result
    for node in document.xpath('//a[contains(@href,"ac=credit")]'):
        match = re.fullmatch(r'积分\s*[:：]\s*([\d,]+)', clean(node))
        if match:
            result['total'] = int(match[1].replace(',', ''))
            break
    area = document.xpath('//*[@id="ct"]')
    area = area[0] if area else document
    match = re.search(r'邪恶指数\s*[:：]\s*([\d,]+)', clean(area))
    if match:
        result['evil'] = int(match[1].replace(',', ''))
    for table in area.xpath('.//table'):
        rows = [[clean(c) for c in row.xpath('./th|./td')] for row in table.xpath('.//tr')]
        if not rows:
            continue
        if rows[0] == ['动作名称', '总次数', '周期次数', '邪恶指数', '门户文章', '最后奖励时间']:
            for row in rows[1:]:
                if len(row) == 6 and row[0] == '每天登录' and re.fullmatch(r'[+-]?\d+', row[3]):
                    datetime.strptime(row[5], '%Y-%m-%d %H:%M')
                    result.update(reward_time=row[5], reward_amount=int(row[3]))
        if rows[0] == ['操作', '积分变更', '详情', '变更时间']:
            for row in rows[1:]:
                if len(row) == 4 and row[0] == '积分规则':
                    amount = re.fullmatch(r'邪恶指数\s*([+-]\d+)', row[1])
                    if amount and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', row[3]):
                        result['ledger'].append({'time': row[3], 'amount': int(amount[1])})
    return result



def evaluate(pages, ended):
    from .core import stamp
    forum, base, system, ledger = pages
    if forum['total'] is None or base['total'] is None or base['evil'] is None:
        raise ValueError('credit fields missing')
    when = stamp(system['reward_time']) if system['reward_time'] else None
    confirmed = bool(when and when.date() == ended.date() and when <= ended
                     and system['reward_amount'] == 1 and any(
                         x['time'] == system['reward_time'] and x['amount'] == 1 for x in ledger['ledger']))
    return {'total': base['total'], 'evil': base['evil'], 'reward_time': system['reward_time'],
            'reward_amount': system['reward_amount'], 'reward_confirmed': confirmed,
            'reward_evidence': '系统奖励及积分流水' if confirmed else None}
