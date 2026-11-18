"""Persistent daily check-in retry budget."""
import copy
import random
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
MAX_ATTEMPTS=5

def stamp(value):
    parsed=datetime.fromisoformat(value)
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)

def state_for(state,now,uid):
    if not isinstance(state,dict) or state.get('day')!=now.date().isoformat() or state.get('uid')!=str(uid):
        return {'day':now.date().isoformat(),'uid':str(uid),'attempts':0,'status':'ready','in_progress':False,'next_retry_at':None,'history':[]}
    value=copy.deepcopy(state)
    count=value.get('attempts')
    if not isinstance(count,int) or isinstance(count,bool) or not 0<=count<=MAX_ATTEMPTS:
        value.update(attempts=MAX_ATTEMPTS,status='blocked',in_progress=False,next_retry_at=None,last_error='重试计数异常，停止自动提交')
    return value

def gate(state,now,uid):
    s=state_for(state,now,uid)
    if s.get('status')=='done':return 'done'
    if s.get('in_progress'):
        try:return 'stale' if now-stamp(s['last_started_at'])>timedelta(minutes=10) else 'running'
        except (ValueError,KeyError,TypeError):return 'blocked'
    if s.get('status')=='blocked':return 'blocked'
    if s['attempts']>=MAX_ATTEMPTS:return 'exhausted'
    if s.get('next_retry_at'):
        try:
            if now<stamp(s['next_retry_at']):return 'cooldown'
        except (ValueError,TypeError):return 'blocked'
    return 'ready'

def reserve(state,now,uid):
    if gate(state,now,uid)!='ready':raise ValueError('retry not due')
    s=state_for(state,now,uid);s['attempts']+=1
    s.update(status='running',in_progress=True,last_started_at=now.strftime('%Y-%m-%d %H:%M:%S'),next_retry_at=None)
    s['attempt_id']=s['day']+'-'+str(uid)+'-'+(str(s['budget_epoch'])+'-' if s.get('budget_epoch') else '')+str(s['attempts'])
    s.setdefault('history',[]).append({'attempt':s['attempts'],'started_at':s['last_started_at'],'status':'running'})
    return s

def finish(state,now,success,reason,delay_seconds=None,stop_for_day=False):
    s=copy.deepcopy(state)
    if delay_seconds is None:delay_seconds=random.SystemRandom().randint(3600,7200)
    if not 3600<=delay_seconds<=7200:raise ValueError('retry delay must be 60-120 minutes')
    s.update(in_progress=False,last_finished_at=now.strftime('%Y-%m-%d %H:%M:%S'),last_error='' if success else str(reason)[:180])
    s['status']='done' if success else 'blocked' if stop_for_day else 'exhausted' if s['attempts']>=MAX_ATTEMPTS else 'waiting'
    s['next_retry_at']=(now+timedelta(seconds=delay_seconds)).strftime('%Y-%m-%d %H:%M:%S') if s['status']=='waiting' else None
    if s.get('history'):
        s['history'][-1].update(status='success' if success else 'failed',finished_at=s['last_finished_at'],reason=s['last_error'],next_retry_at=s['next_retry_at'])
    return s

def permit(state):
    return {'day':state['day'],'uid':state['uid'],'id':state['attempt_id'],'number':state['attempts']}

def failure_reason(result):
    if result.get('checkin_failure_reason'):return str(result['checkin_failure_reason'])[:180]
    stages={'launch':'浏览器启动','balance_before':'签到前余额读取','checkin_before':'签到前账号或状态核验','captcha':'验证码识别/验证','verify_signed':'签到结果核验','balance_after':'到账核验','login':'登录核验'}
    kind=result.get('checkin_error_type') or result.get('error_type')
    stage=stages.get(result.get('checkin_error_stage'),'签到')
    if not result.get('logged_in'):
        attempts=result.get('attempts') or []
        if any(item.get('response_error')=='site_redis_read_error' for item in attempts):
            return '色花堂站点 Redis 缓存读取异常（HTTP 503），未获取或提交验证码'
        codes=[]
        for item in attempts:
            status=str(item.get('status') or '')
            if status.startswith('HTTP_') and status[5:].isdigit() and status[5:] not in codes:codes.append(status[5:])
        if codes:return '站点返回 HTTP '+ '/'.join(codes)+'，未获取或提交验证码'
        return '登录未通过，未提交验证码'+('（'+str(kind)+'）' if kind else '')
    return stage+'未成功'+('（'+str(kind)+'）' if kind else '')


def finish_chiphell(state,now,result):
    success=bool(result.get('logged_in') and result.get('reward_confirmed') and result.get('uid')==state['uid'] and result.get('day')==state['day'])
    waiting=bool(result.get('logged_in') and result.get('reward_check_complete') and not success)
    reason='登录成功，等待奖励周期或到账核验' if waiting else ('登录成功，积分/奖励核验失败：' if result.get('logged_in') else '')+chiphell_failure(result)
    s=finish(state,now,success,reason)
    s['attempt_cooldown_until']=s['next_retry_at']
    if waiting:
        s['last_error']=''
        if s['status']=='waiting':
            s['status']='waiting_reward'
            planned=plan_chiphell(s,now,s['uid'],[result])
            for key in ('window_for_reward','reward_window_open_at','reward_window_estimate','reward_window_basis','next_retry_at'):
                if key in planned:s[key]=planned[key]

    s['last_reason']='奖励已确认，停止当天检查' if success else reason
    if s.get('history'):
        s['history'][-1].update(status='success' if success else 'waiting_reward' if waiting else 'failed',reason=s['last_reason'],next_retry_at=s['next_retry_at'])
    return s

def chiphell_failure(result):
    attempts=result.get('attempts') or []
    reasons=[]
    for item in attempts:
        status=str(item.get('status') or '')
        label={'ReadTimeout':'站点响应超时','ConnectTimeout':'连接站点超时','ConnectionError':'连接站点失败','SSLError':'站点 TLS 连接失败','ResponseTooLarge':'站点响应过大，未解析','account_not_verified':'Cookie 登录态失效或账号未通过核验'}.get(status)
        if status.startswith('HTTP_') and status[5:].isdigit():label='站点返回 HTTP '+status[5:]
        if label and label not in reasons:reasons.append(label)
    if reasons:return '；'.join(reasons)[:180]
    if result.get('error_type'):return {'ValueError':'配置、登录态或页面核验失败','FileNotFoundError':'CookieCloud 数据未找到'}.get(result['error_type'],'登录检查未完成（'+str(result['error_type'])+'）')
    if result.get('source_errors'):return '没有可用的 CookieCloud 登录态，请检查上传和解密配置'
    return '奖励页面或流水未取得完整证据' if result.get('logged_in') else '登录未通过或页面无法核验'


def plan_chiphell(state,now,uid,records):
    """Plan without I/O. The site interval is estimated, never treated as awarded."""
    old=state if isinstance(state,dict) and state.get('uid')==str(uid) else {}
    s=state_for(state,now,uid)
    def parsed(value):
        try:return stamp(value)
        except (ValueError,TypeError):return None
    # The daily allowance resets; a real previous attempt's cooldown must not.
    finished=parsed(old.get('last_finished_at'))
    started=parsed(old.get('last_started_at'))
    if old.get('day')!=s['day']:
        end=max(finished or started,started+timedelta(minutes=10)) if started and old.get('in_progress') else finished
        if end:
            next_time=parsed(old.get('next_retry_at'))
            carry=max(end+timedelta(hours=1),next_time or end)
            if carry>now:s['carry_cooldown_until']=carry.strftime('%Y-%m-%d %H:%M:%S')
    events=[]
    for row in records:
        if not isinstance(row,dict) or not row.get('logged_in') or row.get('uid')!=str(uid):continue
        if not (row.get('reward_confirmed') or row.get('reward_check_complete') and row.get('reward_amount')==1):continue
        when=parsed(row.get('reward_time'))
        if when and when<=now:events.append(when)
    last=max(events) if events else None
    window=None
    if last and last.date()<now.date():
        base=last.replace(minute=0,second=0,microsecond=0)+timedelta(hours=24)
        existing=parsed(s.get('reward_window_open_at'))
        if old.get('window_for_reward')==last.isoformat() and existing and base+timedelta(minutes=5)<=existing<=base+timedelta(minutes=15):window=existing
        else:window=base+timedelta(seconds=random.SystemRandom().randint(300,900))
        s.update(window_for_reward=last.isoformat(),reward_window_open_at=window.strftime('%Y-%m-%d %H:%M:%S'),reward_window_estimate=window.strftime('%Y-%m-%d %H:%M:%S'),reward_window_basis='上次奖励小时整点+暂估24小时，错开5–15分钟；实际周期未公开')
    if s.get('in_progress') or s.get('status') in ('done','blocked','exhausted'):return s
    legacy=old.get('status')=='waiting_reward' and str(old.get('reward_window_basis') or '').startswith('上次奖励+24小时')
    cooling=parsed(s.get('next_retry_at'))
    attempt_cooling=parsed(s.get('attempt_cooldown_until'))
    if not attempt_cooling and old.get('status') in ('waiting','waiting_reward') and not legacy:
        attempt_cooling=parsed(old.get('next_retry_at'))
        if attempt_cooling:s['attempt_cooldown_until']=attempt_cooling.strftime('%Y-%m-%d %H:%M:%S')
    if s.get('status')=='waiting_window':cooling=None;s.update(status='ready',next_retry_at=None)
    if legacy and finished:
        cooling=min(cooling or finished+timedelta(hours=2),finished+timedelta(hours=2))
        attempt_cooling=cooling
        s['attempt_cooldown_until']=cooling.strftime('%Y-%m-%d %H:%M:%S')
    carry=parsed(s.get('carry_cooldown_until'))
    if carry:cooling=max(cooling or carry,carry)
    if attempt_cooling:cooling=max(cooling or attempt_cooling,attempt_cooling)
    if finished and finished<=now:cooling=max(cooling or finished,finished+timedelta(hours=1))
    if window and window>now:
        s.update(status='waiting_window',in_progress=False,next_retry_at=max(window,cooling or window).strftime('%Y-%m-%d %H:%M:%S'),last_reason='未到预计领取窗口，暂不访问，不消耗尝试次数')
    elif cooling and cooling>now:
        s.update(next_retry_at=cooling.strftime('%Y-%m-%d %H:%M:%S'))
        if s.get('status')=='ready':s.update(status='waiting',last_reason='前次实际尝试仍在冷却期，暂不访问')
    return s


def wait_for_login(state,now,uid,reason):
    s=state_for(state,now,uid)
    s.update(status='waiting_login',in_progress=False,
             next_retry_at=(now+timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S'),
             last_error=str(reason)[:180],last_login_checked_at=now.strftime('%Y-%m-%d %H:%M:%S'))
    return s
