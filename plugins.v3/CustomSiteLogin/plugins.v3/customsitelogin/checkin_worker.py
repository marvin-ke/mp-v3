"""Isolated, fixed-origin browser check-in. Credentials enter only over stdin."""
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit,parse_qs
from zoneinfo import ZoneInfo

DEPS=Path('/config/custom-site-login/checkin-deps-v1')
TZ=ZoneInfo('Asia/Shanghai')

def request_window(start_day, deadline, now, monotonic):
    if now.date().isoformat()!=start_day:return '北京时间已跨日，停止本轮提交'
    if monotonic>=deadline:return '本轮执行超时，停止提交'
    return ''

def quota_exhausted(text):
    return any(word in str(text) for word in ('次数已用完','次数超限','验证码获取次数超限','今日验证码已达上限'))

def route_request(route,checkin,*,read_only,captcha_authorized,actions,server_failure,start_day,deadline,current,monotonic):
    req=route.request;u=urlsplit(req.url);q=parse_qs(u.query)
    if u.scheme in ('data','blob'):return route.continue_()
    protected=q.get('mod')==['captcha'] or q.get('ac')==['sign_v2']
    if protected:
        reason=request_window(start_day,deadline,current,monotonic)
        if reason:
            server_failure.update(reason=reason);return route.abort()
    allowed=checkin.allowed_request(req.url,req.method,read_only=read_only,authorised=captcha_authorized,counts=actions)
    if allowed:
        if q.get('mod')==['captcha']:
            actions['captcha_requests' if req.method=='GET' else 'captcha_submissions']+=1
        if q.get('ac')==['sign_v2']:actions['sign_requests']+=1
        return route.continue_()
    return route.abort()

def execute(payload):
    sys.path.insert(0,str(DEPS))
    import checkin
    import sehuatang
    from captcha_solver import CaptchaSolver
    from cloakbrowser import launch
    candidate=payload['candidate'];uid=str(payload['uid']);proxy=payload.get('proxy')
    if not uid.isdigit() or not proxy:raise ValueError('required account/proxy missing')
    origin=sehuatang.origin_for_cookies(candidate['cookies'])
    read_only=bool(payload.get('read_only'));captcha_authorized=False;permit=payload.get('retry_permit') or {}
    checkpoint_path=Path('/config/custom-site-login/checkin-checkpoint.json')
    checkpoint=json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
    if not isinstance(checkpoint,dict):raise ValueError('invalid checkpoint')
    start_day=datetime.now(TZ).date().isoformat();deadline=time.monotonic()+min(170,int(payload.get('timeout',180))-8)
    stages=[];browser=None;context=None;before=None;money_before=None;stage='launch'
    def now():return datetime.now(TZ)
    def check_deadline():
        if time.monotonic()>deadline:raise TimeoutError('deadline')
        if now().date().isoformat()!=start_day:raise RuntimeError('Beijing day changed')
    def safe_page(page):
        check_deadline();u=urlsplit(page.url)
        if u.scheme!='https' or u.hostname not in ('sehuatang.org','www.sehuatang.org'):raise ValueError('foreign origin')
    def snapshot(page):
        safe_page(page);result=checkin.parse_snapshot(page.content(),uid)
        if not result['authenticated']:raise ValueError('account not verified')
        return result
    def visit(page,path):
        check_deadline();response=page.goto(origin+path,wait_until='domcontentloaded',timeout=25000)
        if not response or response.status!=200:raise RuntimeError('page not available')
        safe_page(page);page.wait_for_timeout(500)
    def balance(page):
        visit(page,'home.php?mod=spacecp&ac=credit&op=base')
        parsed=sehuatang.parse_page(page.content().encode())
        if not parsed['logged_in'] or parsed['uid']!=uid:raise ValueError('balance account not verified')
        return parsed['money']
    actions={'captcha_requests':0,'captcha_submissions':0,'sign_requests':0};server_failure={}
    def route(route):
        return route_request(route,checkin,read_only=read_only,captcha_authorized=captcha_authorized,actions=actions,server_failure=server_failure,start_day=start_day,deadline=deadline,current=now(),monotonic=time.monotonic())
    try:
        if not read_only:
            stage='recognition_setup';solver=CaptchaSolver();solver.prepare_click_recognizer()
        stage='launch'
        browser=launch(headless=True,proxy=proxy,args=['--no-sandbox','--disable-dev-shm-usage'],locale='zh-CN',timezone='Asia/Shanghai')
        context=browser.new_context(viewport={'width':1100,'height':900},device_scale_factor=1)
        context.route('**/*',route)
        def response_observed(response):
            u=urlsplit(response.url);q=parse_qs(u.query)
            if u.hostname not in ('sehuatang.org','www.sehuatang.org'):return
            if not (q.get('mod')==['captcha'] or q.get('ac')==['sign_v2']):return
            if response.status!=200:
                server_failure['reason']='验证码或签到接口 HTTP '+str(response.status)
            try:
                value=response.json();message=str(value.get('message') or '')[:160]
                if value.get('data')=='failure' or value.get('code') not in (0,200,None):
                    server_failure['reason']='验证码校验未通过' if q.get('mod')==['captcha'] else '签到接口未成功'
                    server_failure['code']=value.get('code')
                if quota_exhausted(message):
                    server_failure.update(reason='站点验证码配额已用完，停止当日提交',stop=True)
            except Exception:pass
        context.on('response',response_observed)
        cookies=[]
        for c in candidate['cookies']:
            if (c.get('domain') or '').lstrip('.').lower() not in ('sehuatang.org','www.sehuatang.org'):continue
            cookie={k:c[k] for k in ('name','value','domain')};cookie.update(path=c.get('path') or '/',secure=bool(c.get('secure')),httpOnly=bool(c.get('httpOnly')))
            cookies.append(cookie)
        context.add_cookies(cookies);page=context.new_page();page.set_default_timeout(5000)
        stage='balance_before';money_before=balance(page)
        stage='checkin_before';visit(page,'plugin.php?id=dd_sign');before=snapshot(page)
        recovery=checkpoint.get('day')==start_day and checkpoint.get('uid')==uid and checkpoint.get('submission_started') and isinstance(checkpoint.get('money_before'),int) and isinstance(money_before,int) and money_before>checkpoint['money_before']
        if before['state'] not in ('signed','unsigned') and not recovery:raise ValueError('unknown check-in state')
        if before['state']=='unsigned' and not recovery:
            if read_only:raise RuntimeError('read-only verification found unsigned state')
            if not checkin.can_submit(before,checkpoint,start_day,uid,now=now(),permit=permit):raise RuntimeError('pending submission blocks retry')
            check_deadline();current=snapshot(page)
            if current['state']!='unsigned':raise ValueError('check-in state changed before action')
            if permit.get('day')!=start_day or permit.get('uid')!=uid or not isinstance(permit.get('number'),int) or not 1<=permit['number']<=5:raise ValueError('retry permit missing')
            checkpoint={'day':start_day,'uid':uid,'source':candidate['label'],'before':before,'money_before':money_before,'created_at':now().strftime('%Y-%m-%d %H:%M:%S'),'submission_started':True,'provenance':'atomic pre-submit snapshot','attempt_id':permit.get('id'),'attempts_started':permit.get('number')}
            checkpoint_path.parent.mkdir(mode=0o700,exist_ok=True)
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=checkpoint_path.parent,prefix='.checkpoint-',delete=False) as handle:
                os.chmod(handle.name,0o600);json.dump(checkpoint,handle,ensure_ascii=False);handle.flush();os.fsync(handle.fileno());temporary=handle.name
            os.replace(temporary,checkpoint_path)
            directory_fd=os.open(str(checkpoint_path.parent),os.O_RDONLY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
            captcha_authorized=True
            stage='captcha';page.locator('#signin-btn').click(timeout=5000);page.wait_for_timeout(1200)
            for attempt in range(1):
                check_deadline();snapshot(page)
                if quota_exhausted(page.locator('body').inner_text()):
                    server_failure.update(reason='站点验证码配额已用完，停止当日提交',stop=True)
                    raise RuntimeError('captcha quota')
                kind=solver.detect_captcha_type(page);stages.append(kind)
                debug=Path('/config/custom-site-login/captcha-debug');debug.mkdir(mode=0o700,exist_ok=True)
                try:
                    p=debug/(str(attempt)+'-'+kind+'.png');page.locator('#captcha-wrap').screenshot(path=str(p));p.chmod(0o600)
                    meta=page.locator('#captcha-wrap').evaluate('(el) => Array.from(el.querySelectorAll("img, .gc-tile, .gc-drag-block, .gc-drag-slide-bar")).map(e => ({tag:e.tagName,cls:e.className,style:e.getAttribute("style"),naturalWidth:e.naturalWidth,naturalHeight:e.naturalHeight,box:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}}))')
                    logging.info('Captcha layout: %s',json.dumps(meta))
                except Exception:pass
                methods={'slider':solver.solve_slider_captcha,'drag_tile':solver.solve_slider_captcha,'click':solver.solve_click_captcha,'icon_click':solver.solve_icon_click_captcha,'rotate':solver.solve_rotate_captcha}
                if kind=='none':break
                if kind not in methods:raise ValueError('unknown captcha type')
                # Solver success is only an intermediate signal; final evidence follows.
                solved=methods[kind](page);page.wait_for_timeout(1500)
                if solved:break
            # Money is the confirmation source; do not require any post-click button.
            page.wait_for_timeout(1500)
            after={'uid':uid,'authenticated':True,'state':'unknown','days':None,'reward_total':None}
        else:
            after=before
            if checkpoint.get('day')==start_day and checkpoint.get('uid')==uid and checkpoint.get('submission_started') and checkpoint.get('before',{}).get('state')=='unsigned':
                before=checkpoint['before'];money_before=checkpoint.get('money_before')
        captcha_authorized=False
        stage='balance_after'
        money_after=None
        for delay in (0,1500,3000):
            try:
                check_deadline()
                if delay:page.wait_for_timeout(delay)
                money_after=balance(page)
                if (isinstance(money_before,int) and isinstance(money_after,int) and money_after>money_before) or before.get('state')=='signed':break
            except Exception:pass
        if isinstance(money_before,int) and isinstance(money_after,int) and money_after>money_before:
            after=dict(after,uid=uid,authenticated=True)
        # Money confirms the reward, but the balance page does not contain sign-in counters.
        # Always refresh missing counters, including the successful money-increase branch.
        if (after.get('state')!='signed' or after.get('days') is None
                or after.get('reward_total') is None):
            try:
                visit(page,'plugin.php?id=dd_sign&_check='+str(time.time_ns()))
                observed=snapshot(page)
                after=observed
            except Exception:pass  # Counter lookup failure must not undo verified credited money.
        result=checkin.summarise(before=before,after=after,money_before=money_before,money_after=money_after,now=now(),source=candidate['label'],started_day=start_day)
        result['checkin_captcha_types']=stages
        decision=getattr(solver,'last_decision',{}) if 'solver' in locals() else {}
        if decision:
            decision['submission_blocked']=bool(not actions['captcha_submissions'] and decision.get('verification_started') and server_failure.get('reason'))
            result['checkin_recognition']=decision
        if not result['checkin_confirmed']:
            result['checkin_failure_reason']=checkin.recognition_failure_reason(decision,server_failure,actions)
            result['checkin_stop_for_day']=bool(server_failure.get('stop'))
        result['checkin_read_only']=read_only
        if read_only and checkpoint.get('provenance'):
            result['checkin_provenance']={k:checkpoint.get(k) for k in ['provenance','counter_observed_at','money_observation','created_at'] if checkpoint.get(k)}
            result['checkin_evidence']+='；本次只读复核，未重新提交验证码。签到前计数观测：'+str(checkpoint.get('counter_observed_at') or checkpoint.get('created_at') or '检查点')
        result.update({'checkin_'+k:v for k,v in actions.items()})
        if (not read_only and after.get('authenticated') and after.get('uid')==uid
                and after.get('state')=='unsigned' and not actions['captcha_submissions']
                and not actions['sign_requests'] and checkpoint.get('day')==start_day
                and checkpoint.get('uid')==uid):
            checkpoint.update(submission_started=False,terminal_outcome='verified_unsigned_no_submission',
                              captcha_submissions=0,sign_requests=0)
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=checkpoint_path.parent,
                    prefix='.checkpoint-',delete=False) as handle:
                os.chmod(handle.name,0o600);json.dump(checkpoint,handle,ensure_ascii=False)
                handle.flush();os.fsync(handle.fileno());temporary=handle.name
            os.replace(temporary,checkpoint_path)
        if result['checkin_confirmed']:
            proof=Path('/config/custom-site-login/proofs');paths=[]
            stem='sehuatang-'+now().strftime('%Y%m%d-%H%M%S')
            try:
                proof.mkdir(mode=0o700,exist_ok=True);proof.chmod(0o700)
                target=proof/(stem+'-balance.png');page.screenshot(path=str(target));target.chmod(0o600);paths.append(str(target))
            except Exception:result['checkin_proof_status']='unavailable'
            result['checkin_proofs']=paths
        return result
    except Exception as exc:
        # Never expose cookies, URLs containing auth, page content or exception strings.
        return {'day':now().date().isoformat(),'uid':uid,'checkin_checked_at':now().strftime('%Y-%m-%d %H:%M:%S'),'checkin_confirmed':False,'checkin_reward_confirmed':False,'checkin_status':'failed','checkin_observed_state':before.get('state') if before else None,'checkin_observed_authenticated':bool(before and before.get('authenticated')),'checkin_money_before':money_before,'checkin_error_type':type(exc).__name__,'checkin_error_stage':stage,'checkin_failure_reason':server_failure.get('reason') or {'recognition_setup':'点选识别模型未通过启动检查，未获取验证码','launch':'浏览器启动失败','balance_before':'签到前余额或账号读取失败','checkin_before':'签到前状态不明确，未提交验证码','captcha':'验证码处理未完成','verify_signed':'签到结果未能核验','balance_after':'签到后余额核验失败'}.get(stage,'签到未完成'),'checkin_stop_for_day':bool(server_failure.get('stop')),'checkin_evidence':'浏览器签到未取得完整证据；保留独立登录结果','checkin_captcha_types':stages}
    finally:
        if context:
            try:context.close()
            except Exception:pass
        if browser:
            try:browser.close()
            except Exception:pass

if __name__=='__main__':
    payload=json.loads(sys.stdin.read(1024*1024))
    logging.basicConfig(level=logging.INFO,stream=sys.stderr)
    with contextlib.redirect_stdout(sys.stderr):
        try:result=execute(payload)
        except Exception as exc:result={'checkin_confirmed':False,'checkin_reward_confirmed':False,'checkin_status':'failed','checkin_observed_state':before.get('state') if before else None,'checkin_observed_authenticated':bool(before and before.get('authenticated')),'checkin_money_before':money_before,'checkin_error_type':type(exc).__name__}
    print(json.dumps(result,ensure_ascii=False))
