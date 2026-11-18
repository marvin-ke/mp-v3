"""Native MP layouts. Data pages use PageRender events, not FormRender bindings."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from . import core

ENDPOINT = 'plugin/CustomSiteLogin/view'
PAGE_SIZE = 10
MUTED = 'opacity:.68;font-size:12px;line-height:1.6;'
COLORS = {'success':'#2ea87b','failed':'#e46e73','unconfirmed':'#d9a74b','pending':'#8492a6'}


def node(component, content=None, text=None, **props):
    result = {'component': component}
    if content is not None:result['content'] = content
    if text is not None:result['text'] = str(text)
    if props:result['props'] = props
    return result


def button(text, params, *, active=False, icon=None, variant=None):
    props = {'variant': variant or ('tonal' if active else 'text'), 'size':'small', 'style':'text-transform:none;letter-spacing:0;'}
    if icon:props['prepend-icon'] = icon
    result = node('VBtn', text=text, **props)
    result['events'] = {'click': {'api': ENDPOINT, 'method':'POST', 'params':params}}
    return result


def badge(label, state):
    return node('span', text=label, style=f'display:inline-block;color:{COLORS.get(state,COLORS["pending"])};font-size:12px;font-weight:600;line-height:1.5;white-space:normal;')


def value(primary, secondary=None, *, tone=None):
    primary = '—' if primary is None else str(primary)
    children = [node('div',text=primary,style='font-weight:600;font-variant-numeric:tabular-nums;'+('color:'+tone+';' if tone else ''))]
    if secondary:children.append(node('div',text=secondary,style=MUTED))
    return node('div',content=children)


def table(headers, rows, *, label):
    def cell(v, tag):
        props={'style':'padding:13px 14px;text-align:left;vertical-align:top;border-bottom:1px solid rgba(var(--v-theme-on-surface),.08);'+('font-weight:500;opacity:.70;font-size:12px;white-space:nowrap;' if tag=='th' else 'font-size:13px;line-height:1.6;')}
        return node(tag,content=[v] if isinstance(v,dict) else None,text=None if isinstance(v,dict) else ('—' if v is None else v),**props)
    body=[node('tr',content=[cell(v,'td') for v in row]) for row in rows]
    if not body:body=[node('tr',content=[node('td',text='此筛选条件下没有记录',colspan=len(headers),style='padding:32px;text-align:center;opacity:.65;')])]
    return node('div',content=[node('table',content=[node('thead',content=[node('tr',content=[cell(h,'th') for h in headers])]),node('tbody',content=body)],style='width:100%;border-collapse:collapse;',**{'aria-label':label})],style='overflow-x:auto;border:1px solid rgba(var(--v-theme-on-surface),.10);border-radius:8px;')


def state_of(row, today=None):
    if not row or (today and row.get('day') != today):return 'pending','今日待执行' if today else '尚未执行'
    if not row.get('logged_in'):return 'failed','登录失败'
    if row.get('site_id') == 'dian115':
        if row.get('reward_confirmed'):return 'success','签到与所选任务已确认'
        return 'unconfirmed','已登录 · 签到或转盘待完成'
    if row.get('checkin_status')=='failed':
        recognition=row.get('checkin_recognition') or {}
        if recognition and not row.get('checkin_captcha_submissions'):
            if recognition.get('submission_blocked'):return 'unconfirmed','登录成功 · 验证码提交被拦截'
            if recognition.get('accepted') is False and not recognition.get('verification_started'):return 'unconfirmed','登录成功 · 识别不足，未提交签到'
            return 'unconfirmed','登录成功 · 验证码提交未确认'
        return 'unconfirmed','登录成功 · 签到失败'
    if row.get('checkin_reward_confirmed'):return 'success','登录与签到奖励已确认'
    if row.get('checkin_confirmed'):return 'unconfirmed','已签到 · 新增奖励待核验'
    if row.get('reward_confirmed'):return 'success','奖励已确认'
    return 'unconfirmed','登录成功 · 奖励待确认'


def record_id(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()[:16]


def base_params(view, **updates):
    params={key:view[key] for key in ['site_id','section','days','status','page','day','record_id'] if key in view}
    params.update(updates)
    return params


def pages(rows, view):
    total=len(rows);last=max(1,math.ceil(total/PAGE_SIZE));page=min(last,max(1,int(view.get('page') or 1)))
    return rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE],page,last,total


def pager(view, page, last, total):
    items=[node('span',text=f'共 {total} 条 · 第 {page}/{last} 页',style=MUTED)]
    if page>1:items.append(button('上一页',base_params(view,page=page-1,record_id='')))
    if page<last:items.append(button('下一页',base_params(view,page=page+1,record_id='')))
    return node('div',content=items,style='display:flex;align-items:center;justify-content:flex-end;gap:8px;padding:12px 0;flex-wrap:wrap;')


def source_cards(sources):
    cards=[]
    for i,source in enumerate(sources):
        okay=source.get('decrypted',False)
        title='来源 A · MP 已有配置' if source['id']=='primary' else '来源 B · 可选备用来源'
        content=[node('div',text=title,style='font-weight:600;margin-bottom:6px;'),badge(source['message'],'success' if okay else 'failed' if source.get('configured') else 'pending'),
                 node('div',text='最近上传：'+(source.get('uploaded_at') or '暂无上传'),style=MUTED),
                 node('p',text='自动读取 MP 原有 CookieCloud 配置，无需在这里重复填写。' if source['id']=='primary' else '这份来源由本插件独立保存，只在换 KEY / 加密密码时更新。',style=MUTED)]
        cards.append(node('VCol',content=[node('VCard',content=[node('VCardText',content=content)],variant='outlined',style='height:100%;')],cols=12,md=6))
    return node('VRow',content=cards,style='margin-bottom:16px;')


def form(plugin):
    def col(child, md=12):
        return node('VCol', content=[child], cols=12, md=md, style='min-width:0;padding-top:16px;padding-bottom:16px;')

    def field(model, label, **extra):
        hint = extra.pop('hint', '')
        children = [node('VTextField', model=model, label=label, variant='outlined',
                         density='comfortable', **{'hide-details': True, **extra})]
        if hint:
            children.append(node('p', text=hint, style=MUTED + 'margin:10px 0 0;'))
        return node('div', content=children, style='padding-top:10px;margin-bottom:20px;')

    def switch(model, label, hint):
        return node('div', content=[
            node('VSwitch', model=model, label=label, color='primary', **{'hide-details': True}),
            node('p', text=hint, style=MUTED + 'margin:8px 0 0;'),
        ], style='margin-bottom:24px;')

    def note(text, kind='info'):
        return node('VAlert', text=text, type=kind, variant='tonal', density='compact',
                    style='line-height:1.7;margin-bottom:16px;')

    def section(title, subtitle, children):
        heading = [node('h3', text=title, style='font-size:17px;margin:0 0 8px;'),
                   node('p', text=subtitle, style='opacity:.75;line-height:1.7;margin:0 0 18px;')]
        return node('VCard', content=[node('VCardText', content=heading + children)],
                    variant='outlined', style='margin-bottom:18px;')

    def schedule(site_id):
        spec = core.SITES[site_id]
        defaults[site_id + '_cron'] = spec['cron']
        defaults[site_id + '_proxy'] = spec['proxy']
        return node('VExpansionPanels', content=[node('VExpansionPanel', content=[
            node('VExpansionPanelTitle', text='检查时间与网络 · 进阶设置'),
            node('VExpansionPanelText', content=[node('VRow', content=[
                col(field(site_id + '_cron', '定时检查周期（北京时间）',
                          hint=spec['cron'] + ' 表示每天 ' + {'sehuatang':'09:43、21:43','chiphell':'09:37、21:37','dian115':'09:17、21:17'}[site_id] + ' 检查。修改时需使用 Cron 表达式。')),
                col(switch(site_id + '_proxy', '通过 MoviePilot 的代理访问',
                           '代理地址沿用 MP 系统设置。色花堂的验证码签到需要可用代理。' if site_id == 'sehuatang' else '开启后沿用 MP 系统代理；关闭时直接连接本站。')),
            ])]),
        ], value=site_id)], style='margin-top:12px;')

    defaults = {'enabled': False, 'enabled_sites': list(core.SITES), 'notify': False,
                'mac_key': '', 'mac_password': '', 'sehuatang_checkin': False, 'dian115_wheel': False,
                'dian115_signin_mode': 'normal'}
    uids = getattr(plugin, '_uids', {})
    site_select = node('VSelect', model='enabled_sites', label='要自动执行的网站',
                       multiple=True, chips=True,
                       items=[{'title': site['name'], 'value': key} for key, site in core.SITES.items()],
                       variant='outlined', style='margin-top:10px;',
                       **{'closable-chips': True, 'hide-details': True})
    basic = section('1 · 开启任务与选择网站', '操作顺序：同步浏览器 Cookie → 选择网站 → 开启任务 → 保存。', [
        switch('enabled', '开启自动任务（总开关）', '保存后生效。开启时运行所选网站的定时检查；关闭时停止自动任务。'),
        site_select,
        node('p', text='选中后，再按下方对应站点的设置执行。', style=MUTED + 'margin:12px 0 0;'),
    ])
    sehuatang = section('2 · 色花堂', '登录检查和验证码签到是两个步骤，奖励会分别核验。', [
        node('VChip', text='已绑定 UID ' + uids['sehuatang'] if uids.get('sehuatang') not in (None, '0') else '尚未绑定 UID',
             variant='tonal', size='small', style='margin-bottom:16px;'),
        note('① 登录检查：使用 CookieCloud 中的登录态访问网站，核对账号与每日登录奖励。每天登录可获得的奖励以站点记录为准。'),
        switch('sehuatang_checkin', '开启验证码签到', '开启后，在登录检查通过时继续进入签到页，识别验证码、提交签到并核对金钱到账。关闭时仅检查登录与每日登录奖励。'),
        node('p', text='当天已签到就跳过提交。登录失败每 10 分钟检查，不占签到轮数；进入签到后每天最多 5 轮，自动失败重试间隔 60–120 分钟。最终核验账号、签到状态和到账证据。', style=MUTED),
        note('余额示例：上次 116 → 登录后 117（每日登录 +1）→ 签到后 119（签到 +2）。通知中的签到前后余额为 117 → 119，实际奖励以站点为准。'),
        schedule('sehuatang'),
    ])
    chiphell = section('3 · Chiphell', '访问网站领取每日登录奖励，并查询奖励记录确认是否到账。', [
        node('VChip', text='已绑定 UID ' + uids['chiphell'] if uids.get('chiphell') not in (None, '0') else '尚未绑定 UID',
             variant='tonal', size='small', style='margin-bottom:16px;'),
        note('将 Chiphell 加入上方网站列表后，插件会自动检查登录和每日奖励。领取前会根据上次奖励估算可领取时间，尚未到窗口时等待，达到条件后再访问。'),
        node('p', text='窗口内每天最多 5 轮，失败后随机等待 60–120 分钟；确认奖励到账后停止当天检查。页面没有独立的验证码签到开关。', style=MUTED),
        schedule('chiphell'),
    ])
    dian = section('4 · 颠影 MADREEL', '按所选模式签到，并按站点当天已完成次数补足转盘，最多 20 次。', [
        node('VChip', text='已绑定 UID ' + uids['dian115'] if uids.get('dian115') else '尚未绑定 UID',
             variant='tonal', size='small', style='margin-bottom:16px;'),
        note('先在浏览器登录 m.dian115.com 并同步 CookieCloud。当天已签到就跳过，修改签到模式仅对下一次签到生效。'),
        node('VSelect', model='dian115_signin_mode', label='签到模式',
             items=[{'title':'普通签到','value':'normal'},{'title':'幸运签到','value':'lucky'}],
             variant='outlined', density='comfortable', **{'hide-details':True},
             style='margin-top:12px;margin-bottom:20px;'),
        node('p', text='幸运签到使用站点现有的幸运奖励规则；不另行假设中奖概率或保底奖励。', style=MUTED),
        switch('dian115_wheel', '每日转盘补足 20 次', '每次消耗 5 积分，每天最多提交 20 次、消耗 100 积分。已有次数计入当天上限；余额不足、价格改变或提交结果不明时停止。'),
        node('p', text='09:17 执行，21:17 补查。完成后跳过；重试前重新读取站点次数。关闭转盘只执行所选模式签到。转盘在同一任务中逐次核验，不并发抢抽；遇到限流先等待，验证挑战则停止。', style=MUTED),
        schedule('dian115'),
    ])
    credentials = node('VExpansionPanels', content=[node('VExpansionPanel', content=[
        node('VExpansionPanelTitle', text='可选 · 添加或更新另一台电脑的 CookieCloud 来源'),
        node('VExpansionPanelText', content=[node('VRow', content=[
            col(field('mac_key', '来源 B 的 CookieCloud KEY', autocomplete='off',
                      hint='填写另一台电脑扩展使用的完整 KEY；首次添加时应与下方密码一起填写。')),
            col(field('mac_password', '来源 B 的端对端加密密码', type='password', autocomplete='new-password',
                      hint='填写同一套 CookieCloud 的密码。更新已有来源时 KEY 可留空；密码留空时不更新。')),
        ]), note('保存时会先验证该来源能否解密。来源 A 自动读取 MP 已有配置；只有一套来源时，可跳过来源 B。')]),
    ], value='credentials')], style='margin-top:16px;')
    sources = section('5 · 登录凭据与同步状态', '浏览器先登录网站，再通过 CookieCloud 同步到 MP；插件使用同步的 Cookie 验证账号。', [
        source_cards(plugin.source_health()),
        node('p', text='一套来源即可运行。配置多套来源后，插件优先尝试较新的完整登录态，失败时尝试另一套。', style=MUTED),
        credentials,
    ])
    notifications = section('6 · 结果通知', '色花堂发送签到结果；Chiphell 发送登录与奖励结果。', [
        switch('notify', '推送执行结果', '通过 MoviePilot 统一通知，推送到系统中已启用且勾选「站点」消息的所有渠道。'),
        note('接收平台在「MP 设定 → 通知」中统一管理。可同时使用 Telegram、微信等多个渠道。'),
        node('p', text='色花堂每天成功后发送一条签到结果，包含签到奖励、签到前后余额和核验依据。失败进入重试时，可能另有待重试提醒。', style=MUTED),
    ])
    return [node('VContainer', content=[basic, sehuatang, chiphell, dian, sources, notifications],
                 fluid=True, style='padding:4px 2px;max-width:100%;')], defaults


def page(plugin):
    view=plugin.view_state();now=plugin._now();today=now.date().isoformat();sources=plugin.source_health()
    summaries=[]
    for key,spec in core.SITES.items():
        last=core.normalise(plugin.get_data(key+'_last_v1')) or {};history=plugin.get_data(key+'_history_v1') or []
        status,label=state_of(last,today)
        summaries.append({'id':key,'spec':spec,'last':last,'history':history,'status':status,'label':label,'balance':plugin.get_data(key+'_balance_v1') or {}})
    metrics=[('已接入站点',len(summaries)),('今日登录成功',sum(x['status'] in ['success','unconfirmed'] for x in summaries)),('今日奖励确认',sum(x['status']=='success' for x in summaries)),('今日登录异常',sum(x['status']=='failed' for x in summaries))]
    header=node('div',content=[node('div',text='自定义网站 · 登录监控',style='font-size:22px;font-weight:650;letter-spacing:-.3px;'),node('div',text='北京时间 · '+now.strftime('%Y-%m-%d %H:%M:%S'),style=MUTED)],style='margin:0 0 18px;')
    counters=node('VRow',content=[node('VCol',content=[node('VCard',content=[node('VCardText',content=[node('div',text=title,style=MUTED),node('div',text=number,style='font-size:26px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:4px;')])],variant='tonal')],cols=6,md=3) for title,number in metrics],style='margin-bottom:18px;')
    children=[header,counters]
    update=plugin.get_data('source_update_status_v1')
    if update:children.append(node('VAlert',text=update.get('at','')+' · '+update.get('message',''),type='success' if update.get('success') else 'warning',variant='tonal',density='compact',style='margin:0 0 18px;'))
    source_detail=node('details',content=[node('summary',text=f'CookieCloud 来源状态 · {sum(s.get("decrypted",False) for s in sources)}/2 份可解密',style='cursor:pointer;font-size:13px;font-weight:500;padding:12px 0;'),source_cards(sources)],style='margin:4px 0 20px;')
    if not view['site_id']:
        children+=[source_detail,node('h3',text='站点总览',style='font-size:16px;margin:12px 0;'),filters(view,overview=True)]
        rows=sorted(summaries,key=lambda x:(0 if x['status']=='failed' else 1,x['spec']['name']))
        if view['status']!='all':rows=[x for x in rows if matches(x['status'],view['status'])]
        display,p,last,total=pages(rows,view);table_rows=[]
        for item in display:
            spec=item['spec'];latest=item['last'];balance=item['balance']
            table_rows.append([value(spec['name'],'已启用' if item['id'] in plugin._sites else '未启用'),badge(item['label'],item['status']),
                value(balance.get(spec['balance_field']),spec['currency']+' · '+balance.get('day','暂无有效记录')),
                value(latest.get('checked_at'),'来源：'+latest.get('source_label','未取得有效 Cookie')),
                button('查看记录',base_params(view,site_id=item['id'],section='daily',page=1,status='all',day='',record_id=''),icon='mdi-arrow-right')])
        children += [table(['站点','今日状态','最近有效余额','最近检查',''],table_rows,label='站点总览'),pager(view,p,last,total)]
    else:
        item=next(x for x in summaries if x['id']==view['site_id']);spec=item['spec'];history=item['history'];balance=item['balance']
        back=button('所有站点',{'site_id':'','section':'overview','page':1,'status':'all','record_id':'','day':''},icon='mdi-arrow-left')
        children += [node('div',content=[back,node('h2',text=spec['name'],style='font-size:20px;margin:0;'),badge(item['label'],item['status'])],style='display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:8px 0 18px;'),
                     node('div',text=f'最近有效余额：积分 {balance.get("total", "—")} · {spec["currency"]} {balance.get(spec["balance_field"], "—")}（{balance.get("day", "暂无记录")}）',style=MUTED+'margin:0 0 14px;'),
                     node('div',content=[button('每日变化',base_params(view,section='daily',page=1,day='',record_id=''),active=view['section']=='daily',icon='mdi-chart-timeline-variant'),button('执行日志',base_params(view,section='runs',page=1,day='',record_id=''),active=view['section']=='runs',icon='mdi-format-list-bulleted')],style='display:flex;gap:8px;border-bottom:1px solid rgba(var(--v-theme-on-surface),.12);padding:0 0 12px;margin-bottom:12px;'),filters(view)]
        if item['id'] in ('chiphell','sehuatang'):
            budget=plugin.get_data(item['id']+'_retry_v1') or {}
            if budget.get('day')==today and budget.get('uid')==plugin._uids.get(item['id']):
                labels={'waiting_login':'等待登录态更新（不占签到次数）','done':'已成功，今日停止','waiting':'等待重试','waiting_reward':'登录成功，等待奖励','waiting_window':'未到预计领取窗口，不消耗次数','running':'正在执行','exhausted':'已达上限，次日再试','blocked':'今日停止','ready':'待执行'}
                text=f'今日尝试 {budget.get("attempts",0)}/5 · '+labels.get(budget.get('status'),'待核验')
                if budget.get('last_error'):text+='；上次原因：'+budget['last_error']
                elif budget.get('last_reason'):text+='；说明：'+budget['last_reason']
                if budget.get('next_retry_at'):text+='；下次尝试：'+budget['next_retry_at']
                if item['id']=='chiphell' and budget.get('reward_window_estimate'):text+='（领取窗口依据历史记录保守估算）'
                children.append(node('VAlert',text=text,type='success' if budget.get('status')=='done' else 'info',variant='tonal',style='margin-bottom:16px;line-height:1.7;'))
        cutoff=(now.date()-timedelta(days=view['days']-1)).isoformat() if view['days'] else ''
        if view['section']=='daily':
            daily=core.daily_history(history,item['id'],plugin._uids.get(item['id']))
            rows=[r for r in daily if (not cutoff or r['day']>=cutoff) and (view['status']=='all' or matches('failed' if not r['logged_in'] else 'success' if r['reward_status'].startswith('已确认') else 'unconfirmed',view['status']))]
            display,p,last,total=pages(rows,view);rendered=[]
            for row in display:
                def delta(number, previous):
                    if not isinstance(number,int):return '尚无比较基准'
                    label='变化 '+f'{number:+d}'
                    if previous:
                        gap=(datetime.fromisoformat(row['day'])-datetime.fromisoformat(previous)).days
                        label+=' · 较 '+previous+('（跨日累计）' if gap>1 else '')
                    return label
                st='failed' if not row['logged_in'] else 'success' if row['reward_status'].startswith('已确认') else 'unconfirmed'
                rendered.append([value(row['day'],f'{row["attempts_count"]} 次执行'),value(row['total'],delta(row['total_change'],row.get('previous_total_observed_day'))),value(row['balance'],delta(row['balance_change'],row.get('previous_observed_day'))),
                    badge('成功，末次失败' if row['logged_in'] and row['latest_failed'] else '登录成功' if row['logged_in'] else '登录失败','failed' if row['latest_failed'] else st),
                    badge(row['reward_status'],'success' if st=='success' else 'pending'),button('查看当天',base_params(view,section='runs',day=row['day'],page=1,status='all',record_id=''))])
            headers=['日期','总积分',spec['currency'],'登录','登录奖励','']
            if item['id']=='dian115':
                headers=['日期','积分余额','登录','签到与转盘','']
                for rendered_row in rendered:rendered_row.pop(1)
            if item['id']=='sehuatang':
                headers.insert(-1,'签到奖励')
                for rendered_row,day_row in zip(rendered,display):
                    events=[r for r in history if r.get('day')==day_row['day'] and r.get('uid')==plugin._uids.get('sehuatang') and r.get('checkin_status')]
                    confirmed=[r for r in events if r.get('checkin_reward_confirmed')]
                    signed=[r for r in events if r.get('checkin_confirmed')]
                    label='已确认 +'+str(confirmed[-1]['checkin_reward_amount']) if confirmed else '已签到，金额待核验' if signed else '签到失败' if events else '尚无签到记录'
                    rendered_row.insert(-1,badge(label,'success' if confirmed else 'unconfirmed' if signed else 'failed' if events else 'pending'))
            children += [table(headers,rendered,label='每日积分变化'),pager(view,p,last,total),
                node('p',text=('变化按相邻有效观测日计算；签到和转盘完成状态以站点返回为准。本次积分变化可能包含其他操作，不等同于转盘净收益。' if item['id']=='dian115' else '变化按相邻有效观测日计算；缺测日期不记作 0。余额变化可能包含其他操作，登录 +1 单独以站点奖励记录核验。'),style=MUTED)]
        else:
            def when(row):
                try:return core.stamp(row.get('checked_at'))
                except (TypeError,ValueError):return datetime.min.replace(tzinfo=core.TZ)
            ordered=sorted(history,key=when,reverse=True)
            rows=[r for r in ordered if (not cutoff or str(r.get('checked_at',''))[:10]>=cutoff) and (not view['day'] or str(r.get('checked_at',''))[:10]==view['day']) and (view['status']=='all' or matches(state_of(r)[0],view['status']))]
            display,p,last,total=pages(rows,view);rendered=[]
            if view['day']:children.append(node('div',content=[node('span',text='正在查看 '+view['day'],style=MUTED),button('清除日期',base_params(view,day='',page=1,record_id=''))],style='display:flex;align-items:center;gap:8px;'))
            for row in display:
                st,label=state_of(row);normal=core.normalise(row)
                rendered.append([normal.get('checked_at'),badge(label,st),value(row.get('source_label','未取得有效来源'),normal.get('source_update')),
                                 value(row.get(spec['balance_field']),spec['currency']),button('详情',base_params(view,record_id=record_id(row)),icon='mdi-text-box-search-outline')])
            children += [table(['检查时间','结果','Cookie 来源','观测余额',''],rendered,label='执行日志'),pager(view,p,last,total)]
            selected=next((r for r in rows if record_id(r)==view['record_id']),None)
            if selected:
                normal=core.normalise(selected);st,label=state_of(selected)
                details=[node('div',content=[node('h3',text='执行详情',style='font-size:16px;margin:0;'),button('收起',base_params(view,record_id=''))],style='display:flex;justify-content:space-between;align-items:center;'),
                         node('div',text=normal.get('checked_at',''),style=MUTED),badge(label,st),node('p',text='奖励核验：'+(('签到与所选任务已确认' if item['id']=='dian115' else '已确认 +1，'+(normal.get('reward_time') or '')) if selected.get('reward_confirmed') else '尚未取得当天奖励的完整证据'),style='font-size:13px;line-height:1.7;'),
                         node('p',text='执行路径：'+plugin._details(selected),style='font-size:13px;line-height:1.8;overflow-wrap:anywhere;')]
                if item['id'] == 'dian115':
                    details += [node('p', text='签到：'+('今日已确认' if selected.get('signin_confirmed') else '未确认')+
                        '；转盘：'+str(selected.get('wheel_used','未知'))+'/'+str(selected.get('wheel_limit',20))+
                        '；本轮完成：'+str(selected.get('wheel_completed_this_run',0))+' 次', style='font-size:13px;line-height:1.8;'),
                        node('p', text='本轮积分：'+str(selected.get('points_before','未知'))+' → '+str(selected.get('points','未知'))+
                        '；转盘状态：'+{'complete':'今日次数已完成','ready':'待执行','insufficient_points':'积分不足，已停止','price_changed':'价格变化，已停止','submission_uncertain':'上次提交结果不明，已停止','attempt_limit':'已达本地提交上限','invalid_status':'站点次数未通过核验'}.get(selected.get('wheel_status'),'未确认')+
                        ('；只读核验，未提交签到或抽奖' if selected.get('read_only') else ''), style=MUTED)]
                    if selected.get('dian_error'):
                        details.append(node('p', text='未完成原因：'+selected['dian_error'], style=MUTED))
                if selected.get('checkin_status'):
                    label='已确认签到金钱 +'+str(selected.get('checkin_reward_amount')) if selected.get('checkin_reward_confirmed') else '站点显示已签到，新增奖励待核验' if selected.get('checkin_confirmed') else '签到失败：'+str(selected.get('checkin_failure_reason') or selected.get('checkin_error_type','未取得完整证据'))
                    details += [node('p',text='签到：'+label,style='font-size:13px;line-height:1.8;'),node('p',text='签到前后金钱：'+str(selected.get('checkin_money_before','—'))+' → '+str(selected.get('checkin_money_after','—'))+'；累计签到 '+str(selected.get('checkin_days','—'))+' 天；验证码：'+', '.join(selected.get('checkin_captcha_types') or []),style=MUTED)]
                if selected.get('checkin_retry_attempt'):
                    text=f'本日第 {selected["checkin_retry_attempt"]}/5 次'
                    if selected.get('checkin_failure_reason'):text+='；失败原因：'+selected['checkin_failure_reason']
                    text+='；下次尝试：'+(selected.get('checkin_next_retry_at') or '今日停止')
                    details.append(node('p',text=text,style=MUTED+'line-height:1.8;'))
                recognition=selected.get('checkin_recognition') or {}
                if recognition:
                    text=f'图形识别：提示目标 {recognition.get("target_count","未知")} 个，背景候选 {recognition.get("candidate_count","未知")} 个；'+('已提交校验' if selected.get('checkin_captcha_submissions') else '未提交校验')
                    details.append(node('p',text=text,style=MUTED+'line-height:1.8;'))
                if selected.get('login_retry_attempt'):
                    text=f'本日第 {selected["login_retry_attempt"]}/5 次；'+str(selected.get('login_retry_reason') or '')+'；下次检查：'+(selected.get('login_next_retry_at') or '今日停止')
                    details.append(node('p',text=text,style=MUTED+'line-height:1.8;'))
                children.append(node('VCard',content=[node('VCardText',content=details)],variant='outlined',style='margin-top:18px;'))
    children.append(node('div',text='记录保留：每站最近 180 天，最多 240 次执行。切换视图和筛选不会修改历史。',style=MUTED+'margin-top:16px;'))
    return [node('VContainer',content=children,fluid=True,style='padding:6px 0;max-width:100%;')]


def matches(state, selected):
    return (state in ['success','unconfirmed']) if selected=='success' else state==selected


def filters(view, overview=False):
    controls=[]
    if not overview:
        controls += [button(label,base_params(view,days=days,page=1,day='',record_id=''),active=view['days']==days) for label,days in [('近 7 天',7),('近 30 天',30),('全部日期',0)]]
        controls.append(node('span',text='·',style='opacity:.35;padding:0 4px;'))
    controls += [button(label,base_params(view,status=status,page=1,record_id=''),active=view['status']==status) for label,status in [('全部','all'),('登录成功','success'),('登录失败','failed'),('奖励待确认','unconfirmed')]]
    return node('div',content=controls,style='display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin:10px 0 16px;')
