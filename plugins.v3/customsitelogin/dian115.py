"""MADREEL worker boundary and conservative daily quota policy."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ORIGIN = 'https://m.dian115.com'
DOMAINS = ('m.dian115.com', 'dian115.com')


def wheel_plan(status, points, checkpoint):
    used, maximum, cost = (status.get(k) for k in ('used_today', 'max_plays', 'cost'))
    if any(type(x) is not int for x in (used, maximum, cost, points)) or used < 0 or maximum < 0:
        return 'invalid_status'
    if used >= min(20, maximum):
        return 'complete'
    if cost != 5:
        return 'price_changed'
    if checkpoint.get('pending_before') is not None and used <= checkpoint['pending_before']:
        return 'submission_uncertain'
    if checkpoint.get('attempts', 0) >= 20:
        return 'attempt_limit'
    if points < cost:
        return 'insufficient_points'
    return 'ready'

def reconcile_rate_limit(checkpoint, used, now):
    state = dict(checkpoint)
    rejection = state.get('last_rejection') or {}
    pending = state.get('pending_before')
    if (type(pending) is not int or type(used) is not int
            or rejection.get('http') != 429 or rejection.get('code') != 'rate_limited'
            or rejection.get('used_before') != pending
            or now < float(rejection.get('retry_after_at') or 0)):
        return state, False
    if used == pending:
        state['attempts'] = max(0, int(state.get('attempts', 0)) - 1)
        state['rejected_requests'] = state.get('rejected_requests', 0) + 1
    elif used != pending + 1:
        return state, False
    state['pending_before'] = None
    state.pop('last_rejection', None)
    return state, True


def run_worker(candidate, uid, proxy, should_stop=None, read_only=False, wheel=True, timeout=600, signin_mode='normal'):
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('dian115_worker.py'))],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    payload = json.dumps(dict(candidate=candidate, uid=str(uid), proxy=proxy,
                              read_only=read_only, wheel=wheel, signin_mode=signin_mode))
    deadline = time.monotonic() + timeout
    try:
        while True:
            if should_stop and should_stop():
                raise InterruptedError('cancelled')
            if time.monotonic() >= deadline:
                raise TimeoutError('dian115 worker deadline')
            try:
                output, _ = proc.communicate(input=payload, timeout=.5)
                break
            except subprocess.TimeoutExpired:
                payload = None
        if proc.returncode or len(output) > 65536:
            raise RuntimeError('dian115 worker failed')
        result = json.loads(output)
        if result.get('uid') != str(uid):
            raise ValueError('account mismatch')
        return result
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)

