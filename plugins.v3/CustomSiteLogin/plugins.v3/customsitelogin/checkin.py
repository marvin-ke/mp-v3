"""Daily check-in evidence and isolated worker boundary."""
import json
import os
import re
import signal
import subprocess
import sys
import time
import tempfile
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from lxml import html

def parse_snapshot(content, expected_uid):
    text=content.decode('utf-8','replace') if isinstance(content,bytes) else content
    doc=html.fromstring(text)
    uids=set(re.findall(r'\bdiscuz_uid\s*=\s*[\'"]?(\d+)',text))
    uid=next(iter(uids)) if len(uids)==1 else None
    own=False
    for link in doc.xpath('//*[@id="um"]//a[@href]'):
        url=urlsplit(link.get('href'));q=parse_qs(url.query)
        if url.hostname in (None,'sehuatang.org','www.sehuatang.org') and q.get('mod')==['space'] and q.get('uid')==[uid]:own=True
    authenticated=bool(uid==str(expected_uid) and own and doc.xpath('//a[contains(@href,"action=logout")]'))
    for node in doc.xpath('//script|//style|//template|//*[@hidden]|//*[contains(@style,"display:none") or contains(@style,"display: none")]'):
        if node.getparent() is not None:node.drop_tree()
    buttons=doc.xpath('//*[(self::a or self::button) and (@id="signin-btn" or contains(concat(" ", normalize-space(@class), " "), " ddpc_sign_btn_grey "))]')
    state='unknown'
    if len(buttons)==1:
        label=' '.join(buttons[0].text_content().split())
        unsigned='未签到' in label or '点击签到' in label
        signed='今日已签到' in label or label=='已签到'
        if unsigned!=signed:state='unsigned' if unsigned else 'signed'
    clean=' '.join(doc.text_content().split())
    def count(pattern):
        match=re.search(pattern,clean);return int(match[1]) if match else None
    return {'uid':uid,'authenticated':authenticated,'state':state,'days':count(r'累计签到\s*(\d+)\s*天'),'reward_total':count(r'累计获得\s*(\d+)')}

def summarise(*,before,after,money_before,money_after,now,source,started_day=None):
    numeric=lambda v:isinstance(v,int) and not isinstance(v,bool)
    same=before.get('authenticated') and after.get('authenticated') and before.get('uid')==after.get('uid')
    same=bool(same and (started_day is None or started_day==now.date().isoformat()))
    signed=bool(same and after.get('state')=='signed')
    old_signed=before.get('state')=='signed'
    delta=money_after-money_before if numeric(money_before) and numeric(money_after) else None
    old_total,new_total=before.get('reward_total'),after.get('reward_total')
    reward=new_total-old_total if numeric(old_total) and numeric(new_total) else None
    advanced=numeric(before.get('days')) and numeric(after.get('days')) and after['days']==before['days']+1
    confirmed=bool(same and before.get('state')=='unsigned' and numeric(delta) and delta>0)
    signed=bool(signed or confirmed)
    reward=delta if confirmed else None
    return {'day':now.date().isoformat(),'uid':after.get('uid'),'checkin_checked_at':now.strftime('%Y-%m-%d %H:%M:%S'),
            'checkin_confirmed':signed,'checkin_reward_confirmed':confirmed,'checkin_reward_amount':reward if confirmed else None,
            'checkin_money_before':money_before,'checkin_money_after':money_after,'checkin_money_change':delta,
            'checkin_days':after.get('days'),'checkin_reward_total':new_total,'checkin_source':source,
            'checkin_status':'already_signed' if signed and old_signed else 'confirmed' if confirmed else 'signed_reward_unconfirmed' if signed else 'failed',
            'checkin_evidence':'已单独核验登录；同一账号签到前后金钱实际增加，按到账确认成功' if confirmed else '站点显示已签到，本次未取得新增奖励的完整证据' if signed else '未取得已签到证据'}

def done_today(record,now,uid):
    return bool(record and record.get('day')==now.date().isoformat() and record.get('uid')==str(uid) and record.get('checkin_confirmed'))

def can_submit(snapshot,checkpoint,day,uid,*,now=None,permit=None):
    if not (snapshot.get('authenticated') and snapshot.get('uid')==str(uid) and snapshot.get('state')=='unsigned'):return False
    pending=checkpoint.get('day')==day and checkpoint.get('uid')==str(uid) and checkpoint.get('submission_started')
    if not pending:return True
    if not now or not isinstance(permit,dict):return False
    if permit.get('day')!=day or permit.get('uid')!=str(uid) or not permit.get('id') or permit.get('id')==checkpoint.get('attempt_id'):return False
    number=permit.get('number');previous=checkpoint.get('attempts_started',1)
    if not isinstance(number,int) or isinstance(number,bool) or not 1<=number<=5 or number<=previous:return False
    try:
        prior=datetime.fromisoformat(checkpoint['created_at'])
        prior=prior.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if prior.tzinfo is None else prior
        return now-prior>=timedelta(hours=1)
    except (KeyError,TypeError,ValueError):return False

def allowed_request(url,method,*,read_only=False,authorised=False,counts=None):
    u=urlsplit(url);q=parse_qs(u.query)
    if u.scheme!='https' or u.hostname not in ('sehuatang.org','www.sehuatang.org'):return False
    protected=q.get('mod')==['captcha'] or (u.path=='/plugin.php' and q.get('ac')==['sign_v2'])
    if protected and (read_only or not authorised):return False
    counts=counts or {}
    if q.get('mod')==['captcha']:
        key='captcha_requests' if method=='GET' else 'captcha_submissions'
        if counts.get(key,0)>=1:return False
    if q.get('ac')==['sign_v2'] and counts.get('sign_requests',0)>=1:return False
    if q.get('mod')==['post'] or q.get('action') in (['reply'],['newthread'],['logout']):return False
    if method not in ('GET','HEAD'):
        return bool(u.path=='/misc.php' and q.get('mod')==['captcha'] and q.get('action')==['check'] and authorised and not read_only)
    return True

def recognition_failure_reason(decision,server_failure,actions):
    if server_failure.get('stop') and server_failure.get('reason'):return server_failure['reason']
    count=actions.get('captcha_submissions',0)
    rejected=decision.get('accepted') is False and not decision.get('verification_started')
    if count==0 and rejected and decision.get('reason'):return decision['reason']
    if server_failure.get('reason'):return server_failure['reason']
    if count==0:
        return '验证码提交未能核验，未取得签到证据' if decision.get('verification_started') else '验证码识别未得到可靠答案，未提交校验'
    return '签到后未核验到金钱增加，且未能确认今日已签到'


def run_worker(candidate,uid,proxy,should_stop=None,timeout=180,read_only=False,retry_permit=None):
    args=[sys.executable,str(Path(__file__).with_name('checkin_worker.py'))]
    debug=Path('/config/custom-site-login/checkin-worker-last.log')
    debug.parent.mkdir(mode=0o700,exist_ok=True)
    debug_handle=open(debug,'w');os.chmod(debug,0o600)
    process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=debug_handle,text=True,start_new_session=True)
    pending=json.dumps({'candidate':candidate,'uid':str(uid),'proxy':proxy,'timeout':timeout,'read_only':bool(read_only),'retry_permit':retry_permit})
    end=time.monotonic()+timeout
    try:
        while True:
            if should_stop and should_stop():raise InterruptedError('cancelled')
            if time.monotonic()>end:raise TimeoutError('worker deadline')
            try:
                output,_=process.communicate(input=pending,timeout=.5)
                break
            except subprocess.TimeoutExpired:pending=None
        if process.returncode or len(output)>65536:raise RuntimeError('worker failed')
        result=json.loads(output)
        allowed={'day','uid','checkin_checked_at','checkin_confirmed','checkin_reward_confirmed','checkin_reward_amount','checkin_money_before','checkin_money_after','checkin_money_change','checkin_days','checkin_reward_total','checkin_source','checkin_status','checkin_evidence','checkin_captcha_types','checkin_error_type','checkin_error_stage','checkin_proofs','checkin_read_only','checkin_captcha_requests','checkin_captcha_submissions','checkin_sign_requests','checkin_proof_status','checkin_provenance','checkin_failure_reason','checkin_stop_for_day','checkin_recognition','checkin_observed_state','checkin_observed_authenticated'}
        return {k:v for k,v in result.items() if k in allowed}
    finally:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
        debug_handle.close()
        for stream in (process.stdin,process.stdout):
            if stream is not None:stream.close()
