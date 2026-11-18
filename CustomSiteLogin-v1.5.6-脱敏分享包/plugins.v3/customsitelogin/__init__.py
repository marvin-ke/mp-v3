"""One MoviePilot plugin for custom website login and per-site reward history."""
import json
import threading
import re
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.log import logger
from app.runtime.settings import get_runtime_setting
from app.foundation.crypto import CryptoJsUtils, HashUtils
from . import core, ui, checkin, retry, dian115
from fastapi import Body

PRIVATE = Path('/config/custom-site-login/sources.json')
_LOCKS = {key: threading.Lock() for key in core.SITES}
_STATE_LOCK = threading.RLock()


class CustomSiteLogin(_PluginBase):
    plugin_name = '自定义网站自动登录'
    plugin_desc = '集中管理自定义网站登录、每日奖励与分站点日志，自动选择有效的 CookieCloud 来源。'
    plugin_version = '1.5.6'
    plugin_author = 'local'
    author_url = 'https://github.com/jxxghp/MoviePilot'
    plugin_icon = 'mdi-web-check'
    plugin_config_prefix = 'customsitelogin_'
    plugin_order = 99
    auth_level = 2

    def init_plugin(self, config=None):
        with _STATE_LOCK:
            self.stop_service()
            config = dict(config or {})
            mac_key = str(config.pop('mac_key', '') or '').strip()
            mac_password = config.pop('mac_password', '') or ''
            if mac_key or mac_password:
                try:
                    self._apply_local_source(mac_key, mac_password)
                    self.save_data('source_update_status_v1', {'success': True, 'message': '本机 CookieCloud 解密信息已验证并更新', 'at': core.display_time(self._now().isoformat())})
                except ValueError as exc:
                    self.save_data('source_update_status_v1', {'success': False, 'message': str(exc), 'at': core.display_time(self._now().isoformat())})
                finally:
                    # Never retain transient credential fields in public MP plugin config.
                    self.update_config(config)
            self._enabled = bool(config.get('enabled', False))
            self._notify = bool(config.get('notify', False))
            self._checkin_enabled = bool(config.get('sehuatang_checkin', False))
            self._dian_wheel = bool(config.get('dian115_wheel', False))
            self._dian_signin_mode = config.get('dian115_signin_mode', 'normal')
            if self._dian_signin_mode not in ('normal', 'lucky'):
                raise ValueError('颠影签到模式必须为普通或幸运')
            chosen = config.get('enabled_sites', list(core.SITES))
            self._sites = [key for key in core.SITES if key in chosen]
            self._crons = {key: config.get(key + '_cron') or site['cron'] for key, site in core.SITES.items()}
            self._proxies = {key: bool(config.get(key + '_proxy', site['proxy'])) for key, site in core.SITES.items()}
            for value in self._crons.values():
                CronTrigger.from_crontab(value, timezone=core.TZ)
            self._uids = {}
            if PRIVATE.exists():
                data = json.loads(PRIVATE.read_text())
                self._uids = {key: str(data['sites'][key]['expected_uid']) for key in core.SITES if key in data.get('sites', {})}
                if self._enabled:
                    self._migrate_once()

    def get_state(self):
        return self._enabled

    def stop_service(self):
        with _STATE_LOCK:
            self._enabled = False
            self._generation = getattr(self, '_generation', 0) + 1

    @staticmethod
    def _now():
        return datetime.now(core.TZ)

    def _migrate_once(self):
        if self.get_data('migration_v1'):
            return
        pending = {}
        for key, site in core.SITES.items():
            if not site.get('legacy'):
                continue
            for kind in ('last', 'success', 'balance', 'history'):
                dest = key + '_' + kind + '_v1'
                old = self.get_data(kind + '_v1', plugin_id=site['legacy'])
                if old is None:
                    continue
                if kind == 'history':
                    if not isinstance(old, list) or any(not isinstance(row, dict) for row in old):
                        raise ValueError('invalid legacy history')
                    value = [core.normalise(row) for row in old]
                else:
                    if not isinstance(old, dict):
                        raise ValueError('invalid legacy record')
                    value = core.normalise(old)
                existing = self.get_data(dest)
                if existing is not None and existing != value:
                    raise ValueError('migration destination conflict')
                pending[dest] = value
        for dest, value in pending.items():
            self.save_data(dest, value)
        self.save_data('migration_v1', {'completed_at': core.display_time(self._now().isoformat()), 'sites': list(core.SITES)})

    def _source_specs(self, site_id):
        data = json.loads(PRIVATE.read_text())
        uid = str(data['sites'][site_id]['expected_uid'])
        if not uid.isdigit() or uid == '0' or uid != self._uids.get(site_id):
            raise ValueError('invalid account pin')
        if not get_runtime_setting('COOKIECLOUD_ENABLE_LOCAL'):
            raise ValueError('local cookie service disabled')
        sources = []
        secondary = data.get('mac') or {}
        if secondary.get('key') and secondary.get('password'):
            sources.append(dict(secondary, label='备用 CookieCloud 来源'))
        sources.append({'label': 'MP 默认 CookieCloud 来源', 'key': get_runtime_setting('COOKIECLOUD_KEY') or '',
                        'password': get_runtime_setting('COOKIECLOUD_PASSWORD') or ''})
        if len({x['key'] for x in sources}) != len(sources):
            raise ValueError('duplicate source keys')
        return uid, sources

    def _apply_local_source(self, key, password):
        """Validate a new local upload before replacing only the local source secret."""
        if not get_runtime_setting('COOKIECLOUD_ENABLE_LOCAL'):
            raise ValueError('MP 本地 CookieCloud 服务未启用')
        if key and not re.fullmatch(r'[a-zA-Z0-9]{5,128}', key):
            raise ValueError('KEY 格式不正确，请填写 CookieCloud 使用的完整 KEY')
        if key and key == get_runtime_setting('COOKIECLOUD_KEY'):
            raise ValueError('本机 KEY 不能与MP 默认 CookieCloud 来源共用')
        if not isinstance(password, str) or not password:
            raise ValueError('请同时填写本机 CookieCloud 的端对端加密密码')
        try:
            data = json.loads(PRIVATE.read_text())
            key = key or data['mac']['key']
            if not re.fullmatch(r'[a-zA-Z0-9]{5,128}', key) or key == get_runtime_setting('COOKIECLOUD_KEY'):
                raise ValueError('invalid source key')
            path = Path(get_runtime_setting('COOKIE_PATH')) / (key + '.json')
            container = json.loads(path.read_text())
            payload = self._decrypt(container['encrypted'], key, password)
            if not isinstance(payload, dict) or not isinstance(payload.get('cookie_data'), dict):
                raise ValueError('invalid decrypted upload')
        except Exception:
            raise ValueError('新 KEY 或密码无法解开当前上传；请核对后在扩展中重新上传，再保存') from None
        data['mac'] = {'key': key, 'password': password}
        PRIVATE.parent.mkdir(parents=True, exist_ok=True)
        PRIVATE.parent.chmod(0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=PRIVATE.parent, prefix='.source-', delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(handle.name, 0o600)
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, PRIVATE)
        except Exception:
            raise ValueError('凭据文件保存失败，原有配置未主动替换') from None
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _decrypt(encrypted, key, password):
        derived = str(HashUtils.md5(key + '-' + password))[:16].encode()
        return json.loads(CryptoJsUtils.decrypt(encrypted, derived).decode('utf-8'))

    def run_site(self, site_id: str, verify_only=False):
        if site_id not in core.SITES:
            return {'success': False, 'message': '未知站点'}
        if site_id == 'dian115':
            return self._run_dian115(verify_only=verify_only)
        with _STATE_LOCK:
            if not self._enabled or site_id not in self._sites:
                return {'success': False, 'message': '插件或站点未启用'}
            generation = self._generation
        lock = _LOCKS[site_id]
        if not lock.acquire(blocking=False):
            return {'success': False, 'message': '该站点已有检查正在执行'}
        cancelled = lambda: not self._enabled or self._generation != generation
        site = core.SITES[site_id]
        budget = None
        try:
            success = self.get_data(site_id + '_success_v1')
            pending_checkin = verify_only or site_id == 'sehuatang' and self._checkin_enabled and not checkin.done_today(self.get_data('sehuatang_checkin_success_v1'), self._now(), self._uids.get(site_id))
            if core.done_today(success, self._now(), self._uids.get(site_id)) and not pending_checkin:
                with _STATE_LOCK:
                    if cancelled():
                        return {'success': False, 'message': '设置已变更，本次检查已停止'}
                    self._notify_login(site_id, success)
                    if site_id == 'sehuatang' and (self._checkin_enabled or verify_only):
                        self._notify_checkin(self.get_data('sehuatang_checkin_success_v1') or {})
                return {'success': True, 'data': {'site_id': site_id, 'skipped': True, 'message': '今日登录奖励已确认，跳过重复检查'}}
            if site_id == 'chiphell' or site_id == 'sehuatang' and self._checkin_enabled and not verify_only:
                now = self._now()
                state = self.get_data(site_id + '_retry_v1') or {}
                if site_id=='chiphell':
                    planned=retry.plan_chiphell(state,now,self._uids.get(site_id),self._chiphell_reward_records())
                    if planned!=state:self.save_data('chiphell_retry_v1',planned)
                    state=planned
                decision = retry.gate(state, now, self._uids.get(site_id))
                if decision == 'stale':
                    state = retry.finish(state, now, False, '上次任务中断，保留已用次数并延后重试')
                    self.save_data(site_id + '_retry_v1', state)
                    decision = retry.gate(state, now, self._uids.get(site_id))
                if decision != 'ready':
                    return {'success': True, 'data': {'site_id':site_id, 'skipped':True, 'retry_state':decision, 'next_retry_at':state.get('next_retry_at'), 'attempts':state.get('attempts',0), 'message':'未到下次检查时间或今日已停止'}}
                if site_id == 'chiphell':
                    budget = retry.reserve(state, now, self._uids.get(site_id))
                    self.save_data(site_id + '_retry_v1', budget)
            uid, specs = self._source_specs(site_id)
            candidates, errors = core.load_candidates(specs, Path(get_runtime_setting('COOKIE_PATH')), self._decrypt, self._now(), site['domains'])
            proxy = get_runtime_setting('PROXY_HOST') if self._proxies[site_id] else None
            if self._proxies[site_id] and not proxy:
                raise ValueError('configured proxy missing')
            result = core.run_check(site_id, candidates, uid, self._now(), self.get_data(site_id + '_balance_v1'),
                                    should_stop=cancelled, clock=self._now,
                                    proxies={'http': proxy, 'https': proxy} if proxy else None)
            if site_id == 'sehuatang' and (self._checkin_enabled or verify_only) and result.get('logged_in') and not cancelled():
                try:
                    if not verify_only:
                        budget = retry.reserve(state, self._now(), self._uids.get(site_id))
                        self.save_data(site_id + '_retry_v1', budget)
                    chosen = next(c for c in candidates if c['label'] == result.get('source_label'))
                    addition = checkin.run_worker(chosen, uid, proxy, should_stop=cancelled, read_only=verify_only, retry_permit=retry.permit(budget) if budget else None)
                    # Browser failures never discard the independently verified login.
                    if addition.get('day') != result.get('day') or addition.get('uid') != uid or result.get('day') != self._now().date().isoformat():
                        addition = {'checkin_confirmed':False, 'checkin_reward_confirmed':False, 'checkin_status':'failed', 'checkin_error_type':'AccountOrDayMismatch'}
                    result.update({k:v for k,v in addition.items() if k.startswith('checkin_')})
                    if isinstance(addition.get('checkin_money_after'), int):
                        result['money'] = addition['checkin_money_after']
                        old = self.get_data(site_id + '_balance_v1') or {}
                        if isinstance(old.get('money'), int) and not isinstance(old.get('money'), bool) and old.get('uid') in (None, uid):
                            result['money_change'] = result['balance_change'] = result['money'] - old['money']
                    if result.get('day') == self._now().date().isoformat():
                        result['checked_at'] = self._now().isoformat()
                except InterruptedError:
                    return {'success': False, 'message': '设置已变更，本次检查已停止'}
                except Exception as exc:
                    result.update(checkin_confirmed=False, checkin_reward_confirmed=False, checkin_status='failed', checkin_error_type=type(exc).__name__)
            if site_id == 'sehuatang' and (self._checkin_enabled or verify_only) and not result.get('logged_in'):
                result.update(checkin_confirmed=False, checkin_reward_confirmed=False, checkin_status='skipped_login_unverified', checkin_error_stage='login')
                if not verify_only:
                    self.save_data('sehuatang_retry_v1',retry.wait_for_login(state,self._now(),
                        self._uids.get(site_id),'登录未通过，未计入签到尝试；10 分钟后检查登录态'))
            with _STATE_LOCK:
                if cancelled() or result.get('status') == 'cancelled':
                    return {'success': False, 'message': '设置已变更，本次检查已停止'}
                result.update(site_id=site_id, source_errors=errors)
                if budget is not None:
                    (self._finish_chiphell_attempt if site_id == 'chiphell' else self._finish_checkin_attempt)(budget, result)
                    budget = None
                result = core.normalise(result)
                self._record(site_id, result, self._now())
                self._notify_login(site_id, result)
                if site_id == 'sehuatang' and (self._checkin_enabled or verify_only):
                    self._notify_checkin(result)
            if result.get('checkin_status'):
                logger.info('自定义网站自动签到 [色花堂]：' + json.dumps({k:v for k,v in result.items() if k.startswith('checkin_')}, ensure_ascii=False))
            logger.info(f'自定义网站自动登录 [{site["name"]}]：{self._result_text(result)}；检查时间={result["checked_at"]}；来源={result.get("source_label", "无有效来源")}；{site["currency"]}={result.get(site["balance_field"], "未知")}；奖励时间={result.get("reward_time") or "未确认"}')
            return {'success': bool(result.get('logged_in')) and (not (site_id == 'sehuatang' and (self._checkin_enabled or verify_only)) or bool(result.get('checkin_confirmed'))), 'data': result}
        except Exception as exc:
            with _STATE_LOCK:
                if cancelled():
                    return {'success': False, 'message': '设置已变更，本次检查已停止'}
                now = self._now()
                result = {'site_id': site_id, 'day': now.date().isoformat(), 'checked_at': core.display_time(now.isoformat()),
                          'logged_in': False, 'reward_confirmed': False, 'status': 'failed', 'error_type': type(exc).__name__}
                if budget is not None:
                    (self._finish_chiphell_attempt if site_id == 'chiphell' else self._finish_checkin_attempt)(budget, result)
                self._record(site_id, result, now)
            logger.warning(f'自定义网站自动登录 [{site["name"]}]：检查未完成 {type(exc).__name__}')
            return {'success': False, 'message': '检查未完成：' + type(exc).__name__}
        finally:
            lock.release()

    def _run_dian115(self, verify_only=False):
        with _STATE_LOCK:
            if not self._enabled or 'dian115' not in self._sites:
                return {'success': False, 'message': '颠影未启用'}
            generation = self._generation
        lock = _LOCKS['dian115']
        if not lock.acquire(blocking=False):
            return {'success': False, 'message': '颠影已有任务正在执行'}
        cancelled = lambda: not self._enabled or self._generation != generation
        try:
            uid, specs = self._source_specs('dian115')
            old = self.get_data('dian115_success_v1') or {}
            complete = (core.done_today(old, self._now(), uid) and old.get('signin_confirmed')
                        and (not self._dian_wheel or old.get('wheel_used', 0) >= 20))
            if complete and not verify_only:
                self._notify_dian115(dict(old, read_only=False))
                return {'success': True, 'data': {'site_id':'dian115', 'skipped':True,
                                                'message':'今日签到与所选任务已完成'}}
            candidates, errors = core.load_candidates(specs, Path(get_runtime_setting('COOKIE_PATH')),
                self._decrypt, self._now(), core.SITES['dian115']['domains'])
            proxy = get_runtime_setting('PROXY_HOST') if self._proxies['dian115'] else None
            if self._proxies['dian115'] and not proxy:
                raise ValueError('configured proxy missing')
            result = None
            attempts = []
            for candidate in candidates:
                if cancelled():
                    raise InterruptedError('cancelled')
                result = dian115.run_worker(candidate, uid, proxy, should_stop=cancelled,
                                           read_only=verify_only, wheel=self._dian_wheel,
                                           signin_mode=self._dian_signin_mode)
                attempts.append({'source':candidate['label'], 'status':result.get('status','failed')})
                # Never repeat a mutation against another CookieCloud source after login succeeded.
                if result.get('logged_in'):
                    break
            if result is None:
                result = dict(site_id='dian115', uid=uid, day=self._now().date().isoformat(),
                    checked_at=self._now().isoformat(), logged_in=False, reward_confirmed=False,
                    status='failed', dian_error='NoCookieCloudSession')
            result.update(source_errors=errors, attempts=attempts)
            with _STATE_LOCK:
                if cancelled():
                    raise InterruptedError('cancelled')
                self._record('dian115', result, self._now())
                if not verify_only:
                    self._notify_dian115(result)
            logger.info('自定义网站自动登录 [颠影]：' + self._result_text(result) +
                '；转盘=' + str(result.get('wheel_used','未知')) + '/20；积分=' + str(result.get('points','未知')))
            return {'success':bool(result.get('reward_confirmed')), 'data':core.normalise(result)}
        except Exception as exc:
            logger.warning('颠影任务未完成：' + type(exc).__name__)
            return {'success':False, 'message':'颠影任务未完成：' + type(exc).__name__}
        finally:
            lock.release()

    def _notify_dian115(self, result):
        """One verified daily summary via MP; keep submission separate from delivery."""
        uid = self._uids.get('dian115')
        if not (self._enabled and self._notify and result.get('logged_in')
                and not result.get('read_only') and result.get('uid') == uid
                and result.get('day') == self._now().date().isoformat()):
            return
        complete = bool(result.get('reward_confirmed'))
        key = 'dian115_notified_v1'
        old = self.get_data(key) or {}
        fingerprint = [bool(result.get('signin_confirmed')), result.get('wheel_used'),
                       complete, result.get('dian_error')]
        if old.get('day') == result['day'] and old.get('uid') == uid:
            if old.get('complete') or old.get('fingerprint') == fingerprint or not complete:
                return
        history = [r for r in (self.get_data('dian115_history_v1') or [])
                   if r.get('uid') == uid and r.get('day') == result['day']
                   and r.get('logged_in') and not r.get('read_only')]
        reward = result.get('signin_reward_amount')
        if not isinstance(reward, int):
            reward = next((r['signin_reward_amount'] for r in history
                           if type(r.get('signin_reward_amount')) is int), None)
        sign = ('已确认' + (f'，本轮 +{reward} 积分' if isinstance(reward, int) else '，今日已完成')) if result.get('signin_confirmed') else '未确认'
        body = ['签到：' + sign,
                '检查时间：' + (core.display_time(result.get('checked_at')) or '未知')]
        if result.get('wheel_enabled'):
            body.append('今日抽奖：{}/{} 次；本轮完成 {} 次'.format(result.get('wheel_used', '未知'),
                result.get('wheel_limit', 20), result.get('wheel_completed_this_run', 0)))
            if type(result.get('wheel_used')) is int:
                body.append('今日抽奖消耗：{} 积分'.format(result['wheel_used'] * 5))
            if type(result.get('wheel_award_today')) is int:
                body.append('今日抽奖奖励：{} 积分'.format(result['wheel_award_today']))
        before, after = result.get('points_before'), result.get('points')
        baseline = next((r['points_before'] for r in history if type(r.get('points_before')) is int), None)
        if baseline is not None:
            before = baseline
        change = f'（净变化 {after-before:+d}）' if type(before) is int and type(after) is int else ''
        body.extend(['积分余额：{} → {}{}'.format(before if before is not None else '未知',
                     after if after is not None else '未知', change),
                     'Cookie 来源：' + str(result.get('source_label') or '未知')])
        if not complete:
            body.append('未完成原因：' + str(result.get('dian_error') or result.get('wheel_status') or '等待核验'))
        try:
            title = '【颠影】签到与抽奖' + ('完成' if complete else '待处理')
            from app.schemas.message import Message
            self.chain.post_message_strict(
                Message(mtype=NotificationType.SiteMessage, title=title, text='\n'.join(body)),
                event_key='customsitelogin:dian115:{}:{}:{}'.format(uid, result['day'], 'complete' if complete else 'pending'))
            self.save_data(key, {'day': result['day'], 'uid': uid, 'complete': complete,
                'fingerprint': fingerprint, 'status': 'confirmed',
                'submitted_at': core.display_time(self._now().isoformat()), 'reward_confirmed': complete})
            logger.info('颠影签到抽奖汇总：MP渠道严格派发已返回成功')
        except Exception as exc:
            logger.warning('颠影通知提交失败，任务结果已保留：' + type(exc).__name__)

    def verify_dian115(self):
        return self.run_site('dian115', verify_only=True)

    def _finish_chiphell_attempt(self,budget,result):
        state=retry.finish_chiphell(budget,self._now(),result)
        self.save_data('chiphell_retry_v1',state)
        result.update(login_retry_attempt=state['attempts'],login_retry_limit=5,login_next_retry_at=state['next_retry_at'],login_retry_status=state['status'],login_retry_reason=state['last_reason'])
        if state.get('last_error'):result['login_failure_reason']=state['last_error']
        if state.get('reward_window_estimate'):result.update(login_reward_window_estimate=state['reward_window_estimate'],login_reward_window_basis=state['reward_window_basis'])
        logger.info(f'Chiphell 第 {state["attempts"]}/5 次：'+state['last_reason']+'；下次：'+(state['next_retry_at'] or '今日停止'))

    def _chiphell_reward_records(self):
        return [self.get_data('chiphell_'+kind+'_v1') for kind in ('success','balance','last')]

    def retry_chiphell(self):
        if not (self.get_state() and 'chiphell' in self._sites):return {'success':True,'data':{'skipped':True,'reason':'disabled'}}
        now=self._now();state=self.get_data('chiphell_retry_v1') or {};uid=self._uids.get('chiphell')
        if core.done_today(self.get_data('chiphell_success_v1'),now,uid):return {'success':True,'data':{'skipped':True,'reason':'already_done'}}
        if state.get('day')!=now.date().isoformat() or state.get('uid')!=uid:
            records=self._chiphell_reward_records()
            if not any(isinstance(row,dict) and row.get('uid')==uid and row.get('logged_in') for row in records):return {'success':True,'data':{'skipped':True,'reason':'no_pending_retry'}}
        response=self.run_site('chiphell')
        data=response.get('data') or {}
        if data.get('retry_state'):data['reason']=data['retry_state']
        return response

    def _notify_chiphell_reward(self,result):
        now=self._now();uid=self._uids.get('chiphell');old=self.get_data('chiphell_reward_notified_v1') or {}
        if not (self._enabled and self._notify and core.done_today(result,now,uid)):return
        if old.get('day')==now.date().isoformat() and old.get('uid')==uid:return
        body='\n'.join(['登录奖励：已确认 邪恶指数 +1','核验时间：'+core.display_time(now.isoformat()),'站内奖励时间：'+(core.display_time(result.get('reward_time')) or '未知'),'邪恶指数：'+str(result.get('evil','未知')),'总积分：'+str(result.get('total','未知')),'今日奖励已确认，停止当天检查。'])
        try:
            if self._send_checkin_chat('【自定义网站奖励到账】Chiphell',body):self.save_data('chiphell_reward_notified_v1',{'day':now.date().isoformat(),'uid':uid,'status':'submitted','at':core.display_time(now.isoformat())})
        except Exception as exc:logger.warning('Chiphell 奖励通知未确认接收：'+type(exc).__name__)

    def _finish_checkin_attempt(self, budget, result):
        success = bool(result.get('checkin_confirmed'))
        reason = '' if success else retry.failure_reason(result)
        state = retry.finish(budget, self._now(), success, reason, stop_for_day=bool(result.get('checkin_stop_for_day')))
        self.save_data('sehuatang_retry_v1', state)
        result.update(checkin_retry_attempt=state['attempts'], checkin_retry_limit=5,
                      checkin_next_retry_at=state['next_retry_at'], checkin_retry_status=state['status'])
        if not success:result['checkin_failure_reason']=reason
        logger.info(f'色花堂签到第 {state["attempts"]}/5 次：'+('成功，停止当天重试' if success else reason)+'；下次：'+(state['next_retry_at'] or '今日停止'))
        if not success and self._notify:
            body='\n'.join(['检查时间：'+core.display_time(self._now().isoformat()), f'本日尝试：{state["attempts"]}/5', '失败原因：'+reason, '下次尝试：'+(state['next_retry_at'] or '今日停止，次日再检查')])
            try:
                if self._send_checkin_chat('【自定义网站签到待重试】色花堂',body):
                    self.save_data('sehuatang_retry_notice_v1',{'attempt_id':state.get('attempt_id'),'status':'submitted','at':core.display_time(self._now().isoformat())})
            except Exception as exc:logger.warning('色花堂失败通知未确认接收：'+type(exc).__name__)

    def retry_sehuatang(self):
        if not (self.get_state() and self._checkin_enabled and 'sehuatang' in self._sites):
            return {'success':True, 'data':{'skipped':True,'reason':'disabled'}}
        now=self._now();state=self.get_data('sehuatang_retry_v1') or {}
        if state.get('day')!=now.date().isoformat() or state.get('uid')!=self._uids.get('sehuatang'):
            return {'success':True,'data':{'skipped':True,'reason':'no_pending_retry'}}
        if checkin.done_today(self.get_data('sehuatang_checkin_success_v1'),now,self._uids.get('sehuatang')):
            return {'success':True,'data':{'skipped':True,'reason':'already_done'}}
        decision=retry.gate(state,now,self._uids.get('sehuatang'))
        if decision not in ('ready','stale'):
            return {'success':True,'data':{'skipped':True,'reason':decision,'next_retry_at':state.get('next_retry_at')}}
        return self.run_site('sehuatang')

    def verify_sehuatang(self):
        return self.run_site('sehuatang', verify_only=True)

    def run_all(self):
        results = {key: self.run_site(key) for key in self._sites}
        return {'success': self._enabled and all(x['success'] for x in results.values()), 'data': results}

    def _record(self, site_id, result, now):
        prefix = site_id + '_'
        result = core.normalise(result)
        history = self.get_data(prefix + 'history_v1') or []
        retained = []
        for row in history:
            try:
                if core.stamp(row['checked_at']) >= now - timedelta(days=180):
                    retained.append(core.normalise(row))
            except (ValueError, TypeError, KeyError):
                pass
        retained.append(result)
        self.save_data(prefix + 'history_v1', retained[-240:])
        self.save_data(prefix + 'last_v1', result)
        if result.get('logged_in') and isinstance(result.get(core.SITES[site_id]['balance_field']), int):
            self.save_data(prefix + 'balance_v1', result)
        if result.get('reward_confirmed'):
            self.save_data(prefix + 'success_v1', result)
        if site_id == 'sehuatang' and result.get('checkin_confirmed'):
            self.save_data('sehuatang_checkin_success_v1', result)

    def _send_checkin_chat(self, title, body):
        """Submit once to MP; host settings select eligible notification channels."""
        if not self._notify:
            return False
        self.post_message(mtype=NotificationType.SiteMessage, title=title, text=body)
        return True

    def _notify_checkin(self, result):
        if not (self._notify and result.get('checkin_confirmed')
                and result.get('uid') == self._uids.get('sehuatang')
                and result.get('day') == self._now().date().isoformat()):
            return
        old = self.get_data('sehuatang_checkin_notified_v1') or {}
        if (old.get('day'), old.get('uid')) == (result['day'], result['uid']):
            return
        amount = result.get('checkin_reward_amount')
        reward = f'已核验 金钱 +{amount}' if result.get('checkin_reward_confirmed') else '已签到，新增奖励待核验'
        body = '\n'.join([
            '签到：成功', '检查时间：' + result.get('checkin_checked_at', ''),
            '签到奖励：' + reward,
            f'金钱余额：{result.get("checkin_money_before", "未知")} → {result.get("checkin_money_after", "未知")}',
            f'累计签到：{result.get("checkin_days") if result.get("checkin_days") is not None else "未知"} 天；累计签到奖励：{result.get("checkin_reward_total") if result.get("checkin_reward_total") is not None else "未知"}',
            'Cookie 来源：' + result.get('checkin_source', '未知'),
            '奖励证据：' + result.get('checkin_evidence', '')])
        try:
            if not self._send_checkin_chat('【自定义网站签到】色花堂', body):
                logger.warning('色花堂签到结果已保留，但通知未提交到 MP 队列，通知标记未写入')
                return
            self.save_data('sehuatang_checkin_notified_v1', {'day':result['day'], 'uid':result['uid'], 'status':'submitted', 'at':core.display_time(self._now().isoformat())})
            logger.info('色花堂签到结果：已提交 MP 统一通知队列')
        except Exception as exc:
            logger.warning('色花堂签到通知提交失败：' + type(exc).__name__)

    def _notify_login(self, site_id, result):
        """Submit once per Beijing day/account to the existing notification route.

        Host post_message is asynchronous: marker means submitted, not delivered.
        Notification failures must never replace the independently verified login result.
        """
        if site_id == 'sehuatang':
            return
        if not self._enabled or not self._notify:
            return
        now = self._now()
        uid = self._uids.get(site_id)
        if not (result and result.get('logged_in') and uid and result.get('uid') == uid
                and result.get('day') == now.date().isoformat()):
            return
        key = site_id + '_notified_v1'
        old = self.get_data(key) or {}
        if (old.get('day'), old.get('uid')) == (result['day'], uid):
            if site_id=='chiphell' and result.get('reward_confirmed') and not old.get('reward_confirmed'):self._notify_chiphell_reward(result)
            return
        spec = core.SITES[site_id]
        row = core.normalise(result)
        reward = '已确认 +1（' + spec['currency'] + '）' if row.get('reward_confirmed') else '未确认（登录已成功，本次未确认奖励到账）'
        delta = row.get('balance_change')
        suffix = f'（较上次 {delta:+d}）' if isinstance(delta, int) else ''
        body = '\n'.join([
            '登录：成功',
            '检查时间：' + (row.get('checked_at') or '未知'),
            '每日登录奖励：' + reward,
            '积分：' + str(row.get('total', '未知')),
            spec['currency'] + '：' + str(row.get(spec['balance_field'], '未知')) + suffix,
            '奖励时间：' + (row.get('reward_time') or '未确认'),
            'Cookie 来源：' + (row.get('source_label') or '未知'),
        ])
        try:
            if not self._send_checkin_chat('【自定义网站登录成功】' + spec['name'], body):
                return
            self.save_data(key, {'day': result['day'], 'uid': uid,
                                 'submitted_at': core.display_time(now.isoformat()), 'status': 'submitted', 'reward_confirmed':bool(result.get('reward_confirmed'))})
            logger.info(f'自定义网站自动登录 [{spec["name"]}]：已向通知渠道提交今日登录成功通知')
        except Exception as exc:
            logger.warning(f'自定义网站自动登录 [{spec["name"]}]：通知提交失败 {type(exc).__name__}，登录记录已保留')

    def get_service(self):
        if not self.get_state():
            return []
        jobs = [{'id': key, 'name': core.SITES[key]['name'] + ' 自动登录与奖励检查',
                 'trigger': CronTrigger.from_crontab(self._crons[key], timezone=core.TZ),
                 'func': self.run_site, 'func_kwargs': {'site_id': key}} for key in self._sites]
        if 'chiphell' in self._sites:
            jobs.append({'id':'chiphell_retry','name':'Chiphell 失败与奖励重试检查','trigger':CronTrigger.from_crontab('* * * * *',timezone=core.TZ),'func':self.retry_chiphell})
        if self._checkin_enabled and 'sehuatang' in self._sites:
            jobs.append({'id':'sehuatang_retry','name':'色花堂 失败重试检查','trigger':CronTrigger.from_crontab('* * * * *',timezone=core.TZ),'func':self.retry_sehuatang})
        return jobs

    def get_api(self):
        return [{'path':'/verify/dian115','endpoint':self.verify_dian115,'methods':['POST'],'auth':'apikey','summary':'只读核验颠影账号、签到和转盘次数，不消耗积分'},
                {'path':'/retry/chiphell','endpoint':self.retry_chiphell,'methods':['POST'],'auth':'apikey','summary':'按持久冷却与5次上限检查 Chiphell 登录及奖励'},
                {'path':'/retry/sehuatang','endpoint':self.retry_sehuatang,'methods':['POST'],'auth':'apikey','summary':'按持久冷却与5次上限检查待重试任务'},
                {'path': '/verify/sehuatang', 'endpoint': self.verify_sehuatang, 'methods':['POST'], 'auth':'apikey', 'summary':'只读核验色花堂签到，禁止请求或提交验证码'},
                {'path': '/run', 'endpoint': self.run_all, 'methods': ['POST'], 'auth': 'apikey', 'summary': '检查所有已启用站点'},
                {'path': '/run/{site_id}', 'endpoint': self.run_site, 'methods': ['POST'], 'auth': 'apikey', 'summary': '检查指定站点'},
                {'path': '/view', 'endpoint': self.set_view, 'methods': ['POST'], 'auth': 'bear', 'summary': '切换站点、日志视图与筛选'},
                {'path': '/status', 'endpoint': self.status, 'methods': ['GET'], 'auth': 'apikey', 'summary': '读取分站点历史'}]

    def status(self):
        sites = []
        for key, spec in core.SITES.items():
            sites.append({'id': key, 'name': spec['name'], 'enabled': self._enabled and key in self._sites,
                          'currency': spec['currency'], 'daily': core.daily_history(self.get_data(key + '_history_v1') or [], key, self._uids.get(key)), 'notification': self.get_data(key + '_notified_v1'), 'last': core.normalise(self.get_data(key + '_last_v1')),
                          'history': [core.normalise(row) for row in self.get_data(key + '_history_v1') or []]})
        return {'success': True, 'data': {'timezone': 'Asia/Shanghai', 'sites': sites}}

    @staticmethod
    def _result_text(row):
        if row and row.get('site_id') == 'dian115':
            if row.get('reward_confirmed'):
                return '签到与所选任务已确认'
            return '已登录，签到或转盘待完成' if row.get('logged_in') else '登录未确认'
        if row.get('reward_confirmed'):
            return '登录奖励已确认 +1'
        if row.get('logged_in'):
            return '已登录，奖励未确认'
        return '登录失败' if row else '尚未执行'

    def source_health(self):
        """Local read/decrypt diagnostics, never returns keys, passwords or cookies."""
        specs = []
        try:
            specs.append({'id': 'primary', 'key': get_runtime_setting('COOKIECLOUD_KEY') or '',
                          'password': get_runtime_setting('COOKIECLOUD_PASSWORD') or ''})
        except Exception:
            specs.append({'id': 'primary', 'key': '', 'password': ''})
        try:
            private = json.loads(PRIVATE.read_text())
            specs.append(dict(private.get('mac') or {}, id='secondary'))
        except Exception:
            specs.append({'id': 'secondary', 'key': '', 'password': ''})
        results = []
        for spec in specs:
            row = {'id': spec['id'], 'configured': bool(spec.get('key') and spec.get('password')),
                   'decrypted': False, 'uploaded_at': None, 'snapshot_at': None, 'message': '尚未配置'}
            if row['configured']:
                try:
                    key = spec['key']
                    if not re.fullmatch(r'[a-zA-Z0-9]{5,128}', key):
                        raise ValueError('invalid key')
                    path = Path(get_runtime_setting('COOKIE_PATH')) / (key + '.json')
                    if not path.exists():
                        row['message'] = '已配置，尚未收到上传'
                    else:
                        row['uploaded_at'] = datetime.fromtimestamp(path.stat().st_mtime, core.TZ).strftime('%Y-%m-%d %H:%M:%S')
                        envelope = json.loads(path.read_text())
                        data = self._decrypt(envelope['encrypted'], key, spec['password'])
                        if not isinstance(data, dict) or not isinstance(data.get('cookie_data'), dict):
                            raise ValueError('invalid upload')
                        row.update(decrypted=True, message='已接入 · 解密正常', snapshot_at=core.display_time(data.get('update_time')))
                except Exception:
                    row['message'] = '上传存在，解密失败；请核对这份来源的 KEY / 密码' if row['uploaded_at'] else '来源读取失败，请核对配置'
            results.append(row)
        return results

    def view_state(self):
        raw = self.get_data('view_v1') or {}
        state = {'site_id': '', 'section': 'overview', 'days': 7, 'status': 'all', 'page': 1, 'day': '', 'record_id': ''}
        if isinstance(raw, dict):
            state.update({key: raw[key] for key in state if key in raw})
        if state['site_id'] not in core.SITES:
            state.update(site_id='', section='overview')
        if state['section'] not in ['overview', 'daily', 'runs']:
            state['section'] = 'overview'
        if not state['site_id']:
            state['section'] = 'overview'
        if state['status'] not in ['all', 'success', 'failed', 'unconfirmed']:
            state['status'] = 'all'
        if state['days'] not in [0, 7, 30]:
            state['days'] = 7
        try:
            state['page'] = min(9999, max(1, int(state['page'])))
        except (TypeError, ValueError):
            state['page'] = 1
        return state

    def set_view(self, selection: dict = Body(...)):
        if not isinstance(selection, dict):
            return {'success': False, 'message': '视图参数格式错误', 'data': None}
        state = self.view_state()
        for key in state:
            if key in selection:
                state[key] = selection[key]
        if any(not isinstance(state[k],str) for k in ['site_id','section','status','day','record_id']):
            return {'success': False, 'message': '视图标识格式错误', 'data': None}
        if (state['site_id'] and state['site_id'] not in core.SITES) or state['section'] not in ['overview', 'daily', 'runs']:
            return {'success': False, 'message': '未知站点或视图', 'data': None}
        if state['status'] not in ['all', 'success', 'failed', 'unconfirmed'] or state['days'] not in [0, 7, 30]:
            return {'success': False, 'message': '未知筛选条件', 'data': None}
        if not isinstance(state['record_id'], str) or (state['record_id'] and not re.fullmatch(r'[0-9a-f]{16}', state['record_id'])):
            return {'success': False, 'message': '记录标识格式错误', 'data': None}
        try:
            if state['day']:
                datetime.strptime(state['day'], '%Y-%m-%d')
            state['page'] = min(9999, max(1, int(state['page'])))
        except (TypeError, ValueError):
            return {'success': False, 'message': '日期或页码格式错误', 'data': None}
        if not state['site_id'] or state['section'] == 'overview':
            state.update(site_id='', section='overview')
        with _STATE_LOCK:
            self.save_data('view_v1', state)
        return {'success': True, 'message': '', 'data': state}

    def get_form(self):
        return ui.form(self)

    def get_page(self):
        return ui.page(self)

    @staticmethod
    def _details(row):
        labels = {'confirmed': '奖励已确认', 'unconfirmed': '已登录，奖励未确认', 'account_not_verified': '登录态或账号未通过核验',
                  'ReadTimeout': '读取超时', 'ConnectTimeout': '连接超时', 'ConnectionError': '连接失败',
                  'UnicodeDecodeError': 'CookieCloud 解密失败，请核对本机 KEY 与加密密码',
                  'JSONDecodeError': 'CookieCloud 数据解密或解析失败', 'ValueError': '未找到完整有效的本站登录 Cookie，或数据未通过校验'}
        parts = [x.get('source', '') + '：' + labels.get(x.get('status'), '站点请求未完成') for x in row.get('attempts', [])]
        if row.get('error_type'):
            parts.append('检查未完成：' + labels.get(row['error_type'], '检查异常'))
        parts.extend(x.get('source', '') + '：' + labels.get(x.get('reason'), '来源暂不可用') for x in row.get('source_errors', []))
        return '；'.join(parts) or '无额外错误'
