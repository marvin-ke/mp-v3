"""Use the site's own browser client; never solve CAPTCHA or replay signed requests."""
import contextlib
import fcntl
import json
import re
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, '/config/custom-site-login/checkin-deps-v1')
TZ = ZoneInfo('Asia/Shanghai')
ROOT = Path('/config/custom-site-login')
CHECKPOINT = ROOT / 'dian115-checkpoint.json'


def write_checkpoint(value):
    fd, name = tempfile.mkstemp(prefix='.dian115-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, CHECKPOINT)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def execute(payload):
    from cloakbrowser import launch
    from dian115 import ORIGIN, DOMAINS, wheel_plan, reconcile_rate_limit
    uid = str(payload['uid'])
    read_only = bool(payload.get('read_only'))
    wheel = bool(payload.get('wheel'))
    signin_mode = payload.get('signin_mode', 'normal')
    if signin_mode not in ('normal', 'lucky'):
        raise ValueError('InvalidSignInMode')
    day = datetime.now(TZ).date().isoformat()
    deadline = time.monotonic() + 540
    state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
    if state.get('day') != day or state.get('uid') != uid:
        state = dict(day=day, uid=uid, attempts=0, pending_before=None)
    cache = {}
    result = dict(day=day, uid=uid, site_id='dian115', logged_in=False,
                  reward_confirmed=False, status='failed', read_only=read_only,
                  wheel_enabled=wheel, wheel_submitted=0, reward_amount=None)

    def guard():
        if datetime.now(TZ).date().isoformat() != day:
            raise RuntimeError('BeijingDayChanged')
        if time.monotonic() >= deadline:
            raise TimeoutError('deadline')

    browser = launch(headless=True, proxy=payload.get('proxy') or None,
                     args=['--no-sandbox', '--disable-dev-shm-usage'],
                     locale='zh-CN', timezone='Asia/Shanghai')
    try:
        ctx = browser.new_context(viewport={'width': 1100, 'height': 900})
        ctx.add_cookies([dict(name=c['name'], value=c['value'], domain=c['domain'],
                             path=c.get('path') or '/', secure=bool(c.get('secure')),
                             httpOnly=bool(c.get('httpOnly')))
                         for c in payload['candidate']['cookies']
                         if c.get('domain', '').lstrip('.') in DOMAINS])
        permit = {'path': None}

        def route(r):
            req = r.request
            path = urlsplit(req.url).path
            if req.method not in ('GET', 'HEAD', 'OPTIONS'):
                guard()
                if urlsplit(req.url).hostname != 'm.dian115.com':
                    return r.abort()
                if not path.startswith('/api/portal/auth/browser-'):
                    if read_only or path != permit['path']:
                        return r.abort()
                    if path == '/api/portal/signin':
                        body = req.post_data_json
                        actual_mode = body.get('mode', 'normal') if isinstance(body, dict) else None
                        result['signin_request_mode'] = actual_mode
                        if actual_mode != signin_mode:
                            result['signin_mode_mismatch'] = True
                            return r.abort()
                        state['signin_pending'] = True
                        write_checkpoint(state)
                    permit['path'] = None  # at most one mutation per user action
            return r.continue_()

        ctx.route('**/*', route)
        page = ctx.new_page()
        page.set_default_timeout(12000)

        def observe(response):
            u = urlsplit(response.url)
            if u.hostname != 'm.dian115.com':
                return
            if u.path in ('/api/portal/me', '/api/portal/games/status',
                          '/api/portal/signin', '/api/portal/lottery/wheel'):
                try:
                    data = response.json()
                    result.setdefault('api_observed', {})[u.path] = {
                        'http': response.status, 'code': str(data.get('code', ''))[:40],
                        'retry_after': str(response.headers.get('retry-after', ''))[:12]}
                    if response.status == 200 and data.get('code') == 'ok':
                        cache[u.path] = data
                except Exception:
                    pass
        page.on('response', observe)

        def visit(path, required):
            for attempt in range(3):
                guard()
                if attempt:
                    statuses = [result.get('api_observed', {}).get(k, {}) for k in required]
                    if any(s.get('http') in (401, 403) for s in statuses):
                        raise RuntimeError('LoginOrBrowserChallenge')
                    delay = 15 if attempt == 1 else 30
                    for status in statuses:
                        value = status.get('retry_after', '')
                        if value.isdigit():
                            delay = max(delay, min(60, int(value)))
                    result['status_refresh_retries'] = result.get('status_refresh_retries', 0) + 1
                    page.wait_for_timeout(delay * 1000)
                    guard()
                for name in required:
                    cache.pop(name, None)
                response = page.goto(ORIGIN + path, wait_until='domcontentloaded', timeout=35000)
                if not response or response.status != 200:
                    raise RuntimeError('PageUnavailable')
                end = time.monotonic() + 18
                while not all(k in cache for k in required) and time.monotonic() < end:
                    page.wait_for_timeout(200)
                    guard()
                if all(k in cache for k in required):
                    break
            else:
                raise RuntimeError('LoginOrBrowserChallenge')
            user = cache['/api/portal/me']['user']
            if str(user.get('id')) != uid:
                raise ValueError('AccountMismatch')
            return user

        user = visit('/me', ['/api/portal/me'])
        result.update(logged_in=True, points_before=user['points'], points=user['points'],
                      total=user['points'], source_label=payload['candidate']['label'],
                      source_update=payload['candidate']['updated_at'])
        result['signin_already_done'] = str(user.get('last_signin_date', ''))[:10] == day
        if not result['signin_already_done'] and not read_only:
            visit('/me/signin', ['/api/portal/me'])
            guard()
            if state.get('signin_pending'):
                raise RuntimeError('SignInSubmissionUncertain')
            permit['path'] = '/api/portal/signin'
            with page.expect_response(lambda r: urlsplit(r.url).path == '/api/portal/signin', timeout=20000) as pending:
                if signin_mode == 'lucky':
                    page.get_by_role('button', name=re.compile(r'^(?:幸运|运气)签到')).click()
                else:
                    page.get_by_role('button', name='普通签到 稳稳拿到当日签到积分', exact=True).click()
            reply = pending.value.json()
            if pending.value.status != 200 or reply.get('code') != 'ok':
                raise RuntimeError('SignInNotConfirmed')
            # A fresh profile, not the click result, confirms today's completion.
            result['signin_submitted_mode'] = signin_mode
        user = visit('/me/lottery', ['/api/portal/me', '/api/portal/games/status'])
        signed = str(user.get('last_signin_date', ''))[:10] == day
        result['signin_confirmed'] = signed
        result['points_after_signin'] = user['points']
        result['signin_reward_amount'] = (user['points'] - result['points_before']) if signed and not read_only and not result['signin_already_done'] else None
        if signed and state.pop('signin_pending', None) and not read_only:
            write_checkpoint(state)
        if not signed and not read_only:
            raise RuntimeError('SignInNotConfirmed')
        first_used = cache['/api/portal/games/status']['items']['daily_wheel']['used_today']
        result['wheel_before'] = first_used
        if not read_only:
            state, recovered = reconcile_rate_limit(state, first_used, time.time())
            if recovered:
                write_checkpoint(state)
        first_award = cache['/api/portal/games/status']['items']['daily_wheel'].get('award_today')
        while True:
            status = cache['/api/portal/games/status']['items']['daily_wheel']
            reason = wheel_plan(status, user['points'], state)
            result.update(wheel_status=reason, wheel_used=status['used_today'],
                          wheel_limit=min(20, status['max_plays']), wheel_award_today=status.get('award_today'),
                          points=user['points'], total=user['points'])
            if read_only or not wheel or reason != 'ready':
                break
            guard()
            state.update(attempts=state.get('attempts', 0) + 1, pending_before=status['used_today'])
            write_checkpoint(state)
            permit['path'] = '/api/portal/lottery/wheel'
            with page.expect_response(lambda r: urlsplit(r.url).path == '/api/portal/lottery/wheel', timeout=20000) as pending:
                page.get_by_role('button', name='立即抽奖', exact=True).click()
            result['wheel_submitted'] += 1
            reply = pending.value.json()
            if pending.value.status == 429 and reply.get('code') == 'rate_limited':
                state['last_rejection'] = {'http': 429, 'code': 'rate_limited',
                    'used_before': state['pending_before'], 'retry_after_at': time.time() + 60}
                write_checkpoint(state)
                result['rate_limit_retries'] = result.get('rate_limit_retries', 0) + 1
                if result['rate_limit_retries'] > 3 or str(reply.get('limit_window', '')).lower() in ('day', 'daily'):
                    state['last_rejection']['retry_after_at'] = time.time() + 86400
                    write_checkpoint(state)
                    raise RuntimeError('WheelRateLimited')
                delay = max(60, int(reply.get('retry_after') or pending.value.headers.get('retry-after') or 60))
                state['last_rejection']['retry_after_at'] = time.time() + delay
                write_checkpoint(state)
                if delay > 120:
                    state['last_rejection']['retry_after_at'] = time.time() + delay
                    write_checkpoint(state)
                    raise RuntimeError('WheelRateLimited')
                page.wait_for_timeout(delay * 1000)
                guard()
                user = visit('/me/lottery', ['/api/portal/me', '/api/portal/games/status'])
                current = cache['/api/portal/games/status']['items']['daily_wheel']['used_today']
                state, recovered = reconcile_rate_limit(state, current, time.time())
                if recovered:
                    write_checkpoint(state)
                    continue
                raise RuntimeError('WheelCountNotConfirmed')
            if pending.value.status != 200 or reply.get('code') != 'ok':
                raise RuntimeError('WheelResponseUncertain')
            result['wheel_reply_fields'] = {
                str(key): list(value.keys()) if isinstance(value, dict) else type(value).__name__
                for key, value in reply.items()}
            page.wait_for_timeout(1200)
            user = visit('/me/lottery', ['/api/portal/me', '/api/portal/games/status'])
            current = cache['/api/portal/games/status']['items']['daily_wheel']['used_today']
            if current != state['pending_before'] + 1:
                raise RuntimeError('WheelCountNotConfirmed')
            state['pending_before'] = None
            write_checkpoint(state)
        result.update(reward_confirmed=signed and (not wheel or result['wheel_status'] == 'complete'),
                      reward_check_complete=True, balance_change=result['points'] - result['points_before'],
                      wheel_completed_this_run=result['wheel_used'] - first_used)
        completed = result['wheel_completed_this_run']
        result['wheel_spent_this_run'] = completed * 5
        if type(first_award) is int and type(result.get('wheel_award_today')) is int:
            result['wheel_award_this_run'] = result['wheel_award_today'] - first_award
        result['status'] = 'confirmed' if result['reward_confirmed'] else 'unconfirmed'
    except Exception as exc:
        result.update(error_type=type(exc).__name__, dian_error=str(exc) if str(exc) in (
            'BeijingDayChanged','PageUnavailable','LoginOrBrowserChallenge','AccountMismatch',
            'SignInSubmissionUncertain','SignInNotConfirmed','WheelResponseUncertain','WheelCountNotConfirmed','WheelRateLimited') else type(exc).__name__)
    finally:
        browser.close()
    result['checked_at'] = datetime.now(TZ).isoformat()
    return result


if __name__ == '__main__':
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(ROOT / 'dian115-worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(payload)
        print(json.dumps(result, ensure_ascii=False))
