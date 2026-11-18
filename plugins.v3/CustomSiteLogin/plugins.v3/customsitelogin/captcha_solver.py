# Adapted image recognition reference: AWdress/AWBotNest-Plugins
# plugins_v2/awpulse/_core/captcha_solver.py, reviewed 2026-11-02.
# Only the image recognizer is included; no login, reply or posting routines.
# -*- coding: utf-8 -*-
"""
验证码识别模块 —— 本地多算法融合方案
滑块/拼图: ddddocr.slide_match + OpenCV Canny 边缘模板匹配
点选文字: ddddocr 目标检测 + OCR 分类
旋转验证码: 径向能量 + 环带方差对齐
滑动轨迹: 贝塞尔三阶段 + 高斯抖动 + 微回拉
"""

import math
import logging
import random
import re
import time
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:
    import ddddocr
except Exception:
    ddddocr = None


def cleanup_debug_files(debug_dir, patterns, keep=3):
    if not debug_dir:
        return
    try:
        for pattern in patterns:
            files = sorted(
                Path(debug_dir).glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in files[keep:]:
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _bezier_curve(p0, p1, p2, p3, steps):
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def generate_slide_track(distance):
    """贝塞尔三阶段轨迹: 加速 匀速 减速 + 高斯抖动 + 微回拉"""
    distance = int(round(distance))
    if distance <= 0:
        return [{'x': 0, 'y': 0, 't': 20}]

    mid1 = distance * random.uniform(0.55, 0.7)
    mid2 = distance * random.uniform(0.85, 0.95)
    overshoot = distance + random.uniform(2, 5)

    p0 = (0, 0)
    p1 = (mid1 * 0.4, random.uniform(-3, 3))
    p2 = (mid2, random.uniform(-2, 2))
    p3 = (overshoot, random.uniform(-1, 1))

    num_steps = max(20, int(distance / 6))
    raw_points = _bezier_curve(p0, p1, p2, p3, num_steps)

    track = []
    for i, (bx, by) in enumerate(raw_points[1:], 1):
        progress = i / num_steps
        jitter_y = random.gauss(0, 1.2)
        if progress < 0.3:
            dt = random.randint(12, 20)
        elif progress < 0.7:
            dt = random.randint(8, 14)
        else:
            dt = random.randint(18, 32)
        track.append({'x': bx, 'y': by + jitter_y, 't': dt})

    pullback = distance - random.uniform(1, 4)
    track.append({'x': pullback, 'y': random.gauss(0, 0.5), 't': random.randint(30, 50)})
    track.append({'x': float(distance), 'y': random.gauss(0, 0.3), 't': random.randint(20, 35)})
    return track


# ---------------------------------------------------------------------------
# CaptchaSolver
# ---------------------------------------------------------------------------

class CaptchaSolver:
    """本地验证码识别器: ddddocr + OpenCV 多算法融合"""

    def __init__(self, config=None):
        self.config = config or {}
        self._ocr = None
        self._verification_watch = None
        self.last_decision = {}
        self._click_recognizer = None

    def prepare_click_recognizer(self):
        """Load and verify the offline model before fetching a website challenge."""
        if self._click_recognizer is None:
            import importlib.util
            spec=importlib.util.spec_from_file_location('custom_site_click_recognizer',Path(__file__).with_name('click_recognizer.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            self._click_recognizer=module.ClickTextRecognizer()

    @staticmethod
    def _classify_verification_response(response):
        """Return True/False for decisive captcha responses, otherwise None."""
        try:
            url = str(getattr(response, 'url', '') or '').lower()
            resource_type = str(getattr(response.request, 'resource_type', '') or '').lower()
            if resource_type not in {'xhr', 'fetch'}:
                return None
            text = str(response.text() or '')[:8000]
        except Exception:
            return None
        lowered = text.casefold()
        relevant = any(token in url for token in ('captcha', 'verify', 'validate', 'dd_sign', 'go-captcha'))
        relevant = relevant or any(token in lowered for token in (
            'captcha', '验证码', '验证成功', '验证失败', '校验成功', '校验失败', '签到成功',
        ))
        if not relevant:
            return None
        failure_markers = (
            '验证失败', '校验失败', '验证码错误', '验证码失败', '请重试', '再试一次',
            '"success":false', '"success": false', '"code":-1', '"code": -1',
        )
        if any(marker in lowered for marker in failure_markers):
            return False
        success_markers = (
            '验证成功', '校验成功', '签到成功', '"success":true', '"success": true',
            '"code":0', '"code": 0', '"status":"success"', '"status": "success"',
        )
        if any(marker in lowered for marker in success_markers):
            return True
        return None

    def _start_verification_watch(self, page):
        watch = {'results': []}

        def _on_response(response):
            result = self._classify_verification_response(response)
            if result is not None:
                watch['results'].append(result)
                logging.info("验证码服务端结果：%s", "通过" if result else "未通过")

        try:
            page.on('response', _on_response)
            watch['callback'] = _on_response
        except Exception:
            watch['callback'] = None
        self._verification_watch = watch
        return watch

    def _stop_verification_watch(self, page, watch):
        callback = watch.get('callback') if watch else None
        if callback:
            try:
                page.remove_listener('response', callback)
            except Exception:
                pass
        if self._verification_watch is watch:
            self._verification_watch = None

    def _init_ddddocr(self):
        if self._ocr is not None:
            return
        if ddddocr is None:
            raise RuntimeError('ddddocr not installed')
        class LocalOCR:
            def __init__(self):
                self.detector = None
                self.classifier = None
            def detection(self, data):
                if self.detector is None:self.detector = ddddocr.DdddOcr(det=True, ocr=False, show_ad=False)
                return self.detector.detection(data)
            def classification(self, data):
                if self.classifier is None:self.classifier = ddddocr.DdddOcr(det=False, ocr=True, show_ad=False)
                return self.classifier.classification(data)
            def slide_match(self, target, background, **kwargs):
                if self.classifier is None:self.classifier = ddddocr.DdddOcr(det=False, ocr=True, show_ad=False)
                return self.classifier.slide_match(target, background, **kwargs)
        self._ocr = LocalOCR()

    def is_available(self):
        try:
            self._init_ddddocr()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 验证码类型检测
    # ------------------------------------------------------------------

    def detect_captcha_type(self, page):
        try:
            if not self._is_captcha_visible(page):
                return 'none'
            try:
                page.locator('div.gc-loading').wait_for(state='hidden', timeout=6000)
            except Exception:
                pass
            time.sleep(0.5)

            for _ in range(10):
                has_tile = page.locator('div.gc-tile').count() > 0
                has_rotate_pic = page.locator('div.gc-rotate-picture').count() > 0
                has_rotate_thumb = page.locator('div.gc-rotate-thumb').count() > 0
                has_dots = page.locator('div.gc-dots').count() > 0
                has_word = page.locator('div.gc-word').count() > 0
                has_slidebar = page.locator('div.gc-drag-slide-bar').count() > 0
                has_button = page.locator('div.gc-button-block button').count() > 0
                has_imgs = page.locator('#captcha-wrap img').count() > 0

                if has_tile:
                    if has_slidebar:
                        return 'slider'
                    return 'drag_tile'
                if has_rotate_pic or has_rotate_thumb:
                    return 'rotate'
                if has_word:
                    return 'click'
                if has_dots or has_button:
                    return 'icon_click'
                if has_slidebar and has_imgs:
                    return 'slider'
                time.sleep(0.5)
            return 'none'
        except Exception:
            return 'none'

    def _is_captcha_visible(self, page):
        try:
            wrapper = page.locator('div.gc-wrapper')
            for _ in range(12):
                if wrapper.count() > 0 and wrapper.first.is_visible():
                    return True
                time.sleep(0.5)
            return False
        except Exception:
            return False

    def _get_visible_text(self, page, selectors):
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    text = loc.first.inner_text().strip()
                    if text:
                        return text
            except Exception:
                continue
        return ''

    # ------------------------------------------------------------------
    # 滑块 / 拼图块验证码
    # ------------------------------------------------------------------

    def solve_slider_captcha(self, page):
        if not self.is_available():
            return False
        watch = self._start_verification_watch(page)
        try:
            if page.locator('div.gc-tile').count() > 0:
                return self._solve_drag_tile(page)
            return self._solve_slider_handle(page)
        except Exception:
            return False
        finally:
            self._stop_verification_watch(page, watch)

    def _solve_drag_tile(self, page):
        cap = self._capture_slider_images(page)
        if not cap or not cap[0] or not cap[1]:
            return False
        bg_bytes, tgt_bytes, tile_rect = cap
        gap_pos = self._detect_gap_2d(tgt_bytes, bg_bytes, tile_rect)
        if gap_pos is None:
            return False
        gap_x, gap_y = gap_pos
        has_slidebar = page.locator('div.gc-drag-slide-bar').count() > 0
        if has_slidebar:
            if not self._drag_by_displacement(page, gap_x):
                return False
        else:
            if not self._drag_tile_directly(page, gap_x, gap_y):
                return False
        return self._wait_captcha_closed(page)

    def _solve_slider_handle(self, page):
        cap = self._capture_slider_images(page)
        if not cap or not cap[0] or not cap[1]:
            return False
        bg_bytes, tgt_bytes, tile_rect = cap
        gap_pos = self._detect_gap_2d(tgt_bytes, bg_bytes, tile_rect)
        if gap_pos is None:
            return False
        gap_x, _ = gap_pos
        if not self._drag_by_displacement(page, gap_x):
            return False
        return self._wait_captcha_closed(page)

    def _wait_captcha_closed(self, page, timeout=6.0):
        """验证码真正通过的标志: wrapper 持续消失, 而不是失败后被重置成新一题
        失败时 go-captcha 会关闭旧 wrapper 短暂不可见, 然后重新显示新题
        所以需要等更久, 并要求连续多次检测都不可见才判定成功
        """
        deadline = time.time() + timeout
        consecutive_gone = 0
        required = 5  # 连续 5 次 (>= 1.5s) 都不可见才算真消失

        while time.time() < deadline:
            results = (self._verification_watch or {}).get('results', [])
            if False in results:
                return False
            try:
                wrapper = page.locator('div.gc-wrapper')
                if wrapper.count() == 0:
                    consecutive_gone += 1
                else:
                    visible = False
                    try:
                        visible = wrapper.first.is_visible(timeout=200)
                    except Exception:
                        visible = False
                    if visible:
                        # wrapper 又出现了, 说明只是被重置成新一题, 不是真通过
                        return False
                    consecutive_gone += 1
                if consecutive_gone >= required and True in results:
                    return True
            except Exception:
                consecutive_gone += 1
                if consecutive_gone >= required and True in results:
                    return True
            time.sleep(0.3)
        # 没拿到明确的服务端成功响应时返回 False。调用方会重新检测验证码是否仍可见，
        # 并最终以签到页状态为准，避免把弹窗短暂消失误报成通过。
        return False

    def _capture_slider_images(self, page):
        """返回 (bg_bytes_with_tile_masked, tgt_bytes, tile_rect_in_bg_image_coords)
        bg_bytes: 原始 gc-picture 截图, mask 掉 tile 区域避免 ddddocr 把拼图块本身识别成缺口
        tgt_bytes: 拼图块独立截图
        tile_rect: tile 在 bg 图像像素坐标系下的 (x, y, w, h), 用于 mask 和位移计算
        """
        try:
            bg_loc = None
            tgt_loc = None
            for _ in range(10):
                if page.locator('img.gc-picture').count() > 0:
                    bg_loc = page.locator('img.gc-picture').first
                elif page.locator('div.gc-body img').count() > 0:
                    bg_loc = page.locator('div.gc-body img').first

                for sel in ('div.gc-tile img', '.gc-tile img', 'div.gc-tile'):
                    if page.locator(sel).count() > 0:
                        tgt_loc = page.locator(sel).first
                        break

                if bg_loc and tgt_loc:
                    break
                time.sleep(0.3)

            if not bg_loc:
                return None
            bg_bytes = bg_loc.screenshot()
            tgt_bytes = tgt_loc.screenshot() if tgt_loc else None

            tile_rect = None
            if tgt_loc:
                try:
                    bg_box = bg_loc.bounding_box()
                    tile_box = tgt_loc.bounding_box()
                    if bg_box and tile_box:
                        bg_img = Image.open(BytesIO(bg_bytes))
                        scale_x = bg_img.width / bg_box['width']
                        scale_y = bg_img.height / bg_box['height']
                        tx = (tile_box['x'] - bg_box['x']) * scale_x
                        ty = (tile_box['y'] - bg_box['y']) * scale_y
                        tw = tile_box['width'] * scale_x
                        th = tile_box['height'] * scale_y
                        tile_rect = (int(tx), int(ty), int(tw), int(th))
                except Exception:
                    tile_rect = None

            return bg_bytes, tgt_bytes, tile_rect
        except Exception:
            return None

    def _mask_tile_in_bg(self, bg_bytes, tile_rect):
        """把 bg 图像中 tile 占据的矩形用周边平均色填充, 避免被识别为缺口"""
        if not tile_rect:
            return bg_bytes
        try:
            arr = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                return bg_bytes
            x, y, w, h = tile_rect
            x = max(0, x); y = max(0, y)
            w = min(arr.shape[1] - x, w); h = min(arr.shape[0] - y, h)
            if w <= 0 or h <= 0:
                return bg_bytes
            pad = 8
            top = max(0, y - pad); bot = min(arr.shape[0], y + h + pad)
            left = max(0, x - pad); right = min(arr.shape[1], x + w + pad)
            ring = arr[top:bot, left:right].copy()
            inner_y0 = y - top; inner_x0 = x - left
            ring[inner_y0:inner_y0 + h, inner_x0:inner_x0 + w] = 0
            mean_color = ring[ring.sum(axis=2) > 0].mean(axis=0) if (ring.sum(axis=2) > 0).any() else np.array([128, 128, 128])
            arr[y:y + h, x:x + w] = mean_color.astype(np.uint8)
            ok, buf = cv2.imencode('.png', arr)
            return buf.tobytes() if ok else bg_bytes
        except Exception:
            return bg_bytes

    def _detect_gap(self, target_bytes, background_bytes, tile_rect=None):
        """返回 gap_x (缺口左上角 x 坐标, 图像像素)"""
        pos = self._detect_gap_2d(target_bytes, background_bytes, tile_rect)
        return pos[0] if pos else None

    def _detect_gap_2d(self, target_bytes, background_bytes, tile_rect=None):
        """返回 (gap_x, gap_y) 缺口中心坐标 (图像像素)"""
        if not target_bytes:
            return None
        bg_for_match = self._mask_tile_in_bg(background_bytes, tile_rect) if tile_rect else background_bytes

        # 方法1: OpenCV Canny 边缘 + 模板匹配 (返回 x,y)
        try:
            pos = self._cv2_edge_match_2d(target_bytes, bg_for_match, tile_rect)
            if pos is not None:
                return pos
        except Exception:
            pass

        # 方法2: ddddocr slide_match (只返回 x, y 取 tile 高度中心)
        try:
            self._init_ddddocr()
            res = self._ocr.slide_match(target_bytes, bg_for_match, simple_target=True)
            if isinstance(res, dict):
                x = res.get('target_x') or (res.get('target', [None])[0])
                y = res.get('target_y') or (res.get('target', [None, None])[1] if len(res.get('target', [])) > 1 else None)
                if x is not None and x > 0:
                    cand_x = int(x)
                    if tile_rect and abs(cand_x - (tile_rect[0] + tile_rect[2] // 2)) < tile_rect[2] // 2:
                        pass
                    else:
                        tgt_arr = cv2.imdecode(np.frombuffer(target_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
                        cand_y = int(y) if y else (tile_rect[1] + tile_rect[3] // 2 if tile_rect else (tgt_arr.shape[0] // 2 if tgt_arr is not None else 0))
                        return (cand_x, cand_y)
        except Exception:
            pass

        return None

    def _cv2_edge_match_2d(self, target_bytes, background_bytes, tile_rect=None):
        """OpenCV Canny 边缘检测 + matchTemplate, 返回 (center_x, center_y)"""
        bg_arr = cv2.imdecode(np.frombuffer(background_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
        tgt_arr = cv2.imdecode(np.frombuffer(target_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
        if bg_arr is None or tgt_arr is None:
            return None
        tgt_h, tgt_w = tgt_arr.shape
        bg_h, bg_w = bg_arr.shape
        if tgt_h > bg_h or tgt_w > bg_w:
            return None

        bg_edge = cv2.Canny(cv2.GaussianBlur(bg_arr, (5, 5), 0), 50, 150)
        tgt_edge = cv2.Canny(cv2.GaussianBlur(tgt_arr, (5, 5), 0), 50, 150)

        if tile_rect:
            tx, ty, tw, th = tile_rect
            pad = 4
            x1 = max(0, tx - pad); y1 = max(0, ty - pad)
            x2 = min(bg_w, tx + tw + pad); y2 = min(bg_h, ty + th + pad)
            bg_edge[y1:y2, x1:x2] = 0

        result = cv2.matchTemplate(bg_edge, tgt_edge, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < 0.2:
            return None
        # 返回匹配区域的左上角 + 半个 tile 宽高 = 中心
        # go-captcha tile 的 left/top 是左上角对齐, 所以返回左上角
        return (int(max_loc[0]), int(max_loc[1]))

    def _drag_by_displacement(self, page, gap_x):
        try:
            tile_loc = page.locator('div.gc-tile').first
            if tile_loc.count() == 0:
                return False
            tile_box = tile_loc.bounding_box()
            bg_loc = page.locator('img.gc-picture').first
            bg_box = bg_loc.bounding_box()
            if not tile_box or not bg_box:
                return False
            bg_img = Image.open(BytesIO(bg_loc.screenshot()))
            scale_x = bg_box['width'] / bg_img.width
            # gap_x 是缺口左上角 x (图像像素), 转为屏幕坐标
            gap_screen_x = bg_box['x'] + gap_x * scale_x
            # tile 当前左上角屏幕坐标
            tile_screen_x = tile_box['x']
            displacement = gap_screen_x - tile_screen_x

            handle = page.locator('div.gc-drag-block').first
            handle_box = handle.bounding_box()
            if not handle_box:
                return False
            start_x = handle_box['x'] + handle_box['width'] / 2
            start_y = handle_box['y'] + handle_box['height'] / 2
            return self._drag_by_track(page, start_x, start_y, start_x + displacement, start_y)
        except Exception:
            return False

    def _drag_tile_directly(self, page, gap_x, gap_y):
        """直接拖拽 gc-tile 到缺口位置 (无底部滑块的拼图类型)"""
        try:
            tile_loc = page.locator('div.gc-tile').first
            if tile_loc.count() == 0:
                return False
            tile_box = tile_loc.bounding_box()
            bg_loc = page.locator('img.gc-picture').first
            bg_box = bg_loc.bounding_box()
            if not tile_box or not bg_box:
                return False
            bg_img = Image.open(BytesIO(bg_loc.screenshot()))
            scale_x = bg_box['width'] / bg_img.width
            scale_y = bg_box['height'] / bg_img.height

            # gap_x/gap_y 是缺口左上角 (图像像素), 转为屏幕坐标
            target_x = bg_box['x'] + gap_x * scale_x + tile_box['width'] / 2
            target_y = bg_box['y'] + gap_y * scale_y + tile_box['height'] / 2

            # tile 当前中心
            start_x = tile_box['x'] + tile_box['width'] / 2
            start_y = tile_box['y'] + tile_box['height'] / 2

            return self._drag_by_track_2d(page, start_x, start_y, target_x, target_y)
        except Exception:
            return False

    def _drag_by_track_2d(self, page, start_x, start_y, end_x, end_y):
        """2D 拖拽: 同时有 x 和 y 方向位移"""
        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.08, 0.16))
        page.mouse.down()

        dx = end_x - start_x
        dy = end_y - start_y
        num_steps = max(15, int(abs(dx) / 8))
        for i in range(1, num_steps + 1):
            progress = i / num_steps
            # ease-in-out
            t = progress * progress * (3 - 2 * progress)
            cx = start_x + dx * t + random.gauss(0, 0.8)
            cy = start_y + dy * t + random.gauss(0, 0.8)
            page.mouse.move(cx, cy, steps=2)
            time.sleep(random.uniform(0.01, 0.025))

        page.mouse.move(end_x, end_y, steps=3)
        time.sleep(random.uniform(0.05, 0.1))
        page.mouse.up()
        time.sleep(random.uniform(0.3, 0.6))
        return True

    # ------------------------------------------------------------------
    # 图标点选验证码 (gc-dots, 按 header 提示顺序点击大图中的图标)
    # ------------------------------------------------------------------

    def _reject_icon(self, reason, **details):
        self.last_decision.update(accepted=False, reason=reason, **details)
        logging.warning('图形点选保护：%s', self.last_decision)
        return False

    def solve_icon_click_captcha(self, page):
        self.last_decision={'kind':'image_click','accepted':False,'verification_started':False}
        if not self.is_available():
            return self._reject_icon('本地图形识别不可用，未提交校验')
        watch = self._start_verification_watch(page)
        try:
            self.prepare_click_recognizer()
            bg_loc=page.locator('img.gc-picture').first
            header_loc=page.locator('div.gc-header img').first
            confirm=page.locator('div.gc-button-block button').first
            if not bg_loc.count() or not header_loc.count() or not confirm.count():
                return self._reject_icon('图形点选界面不完整，未提交校验')
            bg_box=bg_loc.bounding_box();bg_bytes=bg_loc.screenshot();header_bytes=header_loc.screenshot()
            if not bg_box or not bg_bytes or not header_bytes:
                return self._reject_icon('图形点选图片读取失败，未提交校验')
            templates=self._split_header_icons(header_bytes)
            self.last_decision['target_count']=len(templates)
            if not templates:
                return self._reject_icon('提示目标数量或分割不可靠，未提交校验')
            boxes=self._ocr.detection(bg_bytes)
            bg_img=Image.open(BytesIO(bg_bytes));candidates=[]
            for box in boxes or []:
                if len(box)!=4:continue
                x1,y1,x2,y2=map(int,box)
                if x1<0 or y1<0 or x2>bg_img.width or y2>bg_img.height or x2-x1<8 or y2-y1<8:continue
                candidates.append({'cx':(x1+x2)/2,'cy':(y1+y2)/2,'box':(x1,y1,x2,y2)})
            self.last_decision['candidate_count']=len(candidates)
            if len(candidates)<len(templates):
                return self._reject_icon('背景可用目标不足，未提交校验')
            header=cv2.imdecode(np.frombuffer(header_bytes,np.uint8),cv2.IMREAD_COLOR)
            background=cv2.imdecode(np.frombuffer(bg_bytes,np.uint8),cv2.IMREAD_COLOR)
            regions=self._validated_header_regions(cv2.cvtColor(header,cv2.COLOR_BGR2RGB))
            answer=self._click_recognizer.recognize(header,background,[c['box'] for c in candidates],regions)
            self.last_decision.update({k:answer[k] for k in ('method','labels','target_scores','candidate_scores','seconds') if k in answer})
            if not answer.get('accepted'):
                return self._reject_icon(answer.get('reason','点选文字未完整识别')+'，未刷新或提交校验')
            selected=answer.get('selected',[])
            if len(selected)!=len(templates) or len(set(selected))!=len(selected) or any(not isinstance(i,int) or not 0<=i<len(candidates) for i in selected):
                return self._reject_icon('点选答案数量或位置不可靠，未提交校验')
            for offset,left in enumerate(selected):
                a=candidates[left]['box']
                for right in selected[offset+1:]:
                    b=candidates[right]['box'];overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
                    if overlap>0:
                        return self._reject_icon('背景目标重叠，无法确认点击唯一性，未提交校验')
            if bg_loc.screenshot()!=bg_bytes or header_loc.screenshot()!=header_bytes:
                return self._reject_icon('验证码图片已变化，停止旧答案提交')
            bg_box=bg_loc.bounding_box()
            if not bg_box or not all(math.isfinite(float(bg_box[k])) for k in ('x','y','width','height')) or min(bg_box['width'],bg_box['height'])<=0:
                return self._reject_icon('当前验证码位置不可靠，未提交校验')
            scale_x=bg_box['width']/bg_img.width;scale_y=bg_box['height']/bg_img.height
            positions=[(bg_box['x']+candidates[i]['cx']*scale_x,bg_box['y']+candidates[i]['cy']*scale_y) for i in selected]
            for offset,a in enumerate(positions):
                if not all(math.isfinite(float(v)) for v in a):return self._reject_icon('点击坐标不可靠，未提交校验')
                for b in positions[offset+1:]:
                    if math.hypot(a[0]-b[0],a[1]-b[1])<8:return self._reject_icon('点击位置无法可靠区分，未提交校验')
            # Validate every target before the first mouse action. Never force a best guess.
            self.last_decision.update(accepted=True,reason='完整提示字与背景字唯一匹配',min_similarity=min(answer.get('candidate_scores') or [0]))
            for px,py in positions:
                page.mouse.click(px,py);time.sleep(random.uniform(.25,.45))
            time.sleep(random.uniform(.2,.4))
            self.last_decision['verification_started']=True
            confirm.click()
            return self._wait_captcha_closed(page)
        except Exception as exc:
            suffix='验证结果未明确' if self.last_decision.get('verification_started') else '未提交校验'
            return self._reject_icon('图形点选处理异常，'+suffix,error_type=type(exc).__name__)
        finally:
            self._stop_verification_watch(page,watch)

    @staticmethod
    def _select_icon_matches(scores):
        selected=[]
        for row in scores:
            if len(row)<2 or not all(math.isfinite(float(v)) for v in row):return None
            ranked=sorted(range(len(row)),key=lambda i:row[i],reverse=True)
            top,second=ranked[:2]
            # Conservative refusal thresholds; not a calibrated probability of success.
            if row[top]<.80 or row[top]-row[second]<.15 or top in selected:return None
            selected.append(top)
        return selected if len(selected)>=2 else None

    @staticmethod
    def _header_regions(rgb,kernel):
        hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
        mask=((hsv[:,:,1]>65)&(hsv[:,:,2]<245)).astype(np.uint8)*255
        opened=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones(kernel,np.uint8))
        _,_,stats,_=cv2.connectedComponentsWithStats(opened)
        h,w=mask.shape;regions=[]
        for x,y,cw,ch,area in stats[1:]:
            if area<max(36,h*.9) or ch<max(12,h*.35) or cw<6 or cw/ch>2.0:continue
            regions.append((int(x),int(y),int(x+cw),int(y+ch)))
        merged=[]
        for box in sorted(regions):
            if merged and box[0]<=merged[-1][2]+2:
                p=merged[-1];merged[-1]=(p[0],min(p[1],box[1]),max(p[2],box[2]),max(p[3],box[3]))
            else:merged.append(box)
        return merged

    def _validated_header_regions(self,rgb):
        primary=self._header_regions(rgb,(2,2));secondary=self._header_regions(rgb,(2,1))
        if not 2<=len(secondary)<=6:return []
        if len(primary)!=len(secondary):
            # A square opening can erase thin glyphs. Require agreement from two
            # vertical openings and ensure the square-opening boxes form a subset.
            third=self._header_regions(rgb,(3,1))
            if len(primary)>len(secondary) or len(third)!=len(secondary):return []
            def supported(a,b):
                ix=max(0,min(a[2],b[2])-max(a[0],b[0]));iy=max(0,min(a[3],b[3])-max(a[1],b[1]))
                aa=(a[2]-a[0])*(a[3]-a[1]);bb=(b[2]-b[0])*(b[3]-b[1])
                return (min(aa,bb)>0 and ix*iy/min(aa,bb)>=.85 and min(aa,bb)/max(aa,bb)>=.25
                        and abs((a[0]+a[2])-(b[0]+b[2]))<=max(a[2]-a[0],b[2]-b[0]))
            if any(not supported(a,b) for a,b in zip(secondary,third)):return []
            if any(sum(supported(a,b) for b in secondary)!=1 for a in primary):return []
            return secondary
        regions=[]
        for a,b in zip(primary,secondary):
            ix=max(0,min(a[2],b[2])-max(a[0],b[0]));iy=max(0,min(a[3],b[3])-max(a[1],b[1]))
            union=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-ix*iy
            area_a=(a[2]-a[0])*(a[3]-a[1]);area_b=(b[2]-b[0])*(b[3]-b[1])
            # Thin detached strokes change the tight box without changing its glyph.
            # Require strong containment AND nearby centers AND similar box area.
            near=(abs((a[0]+a[2])-(b[0]+b[2]))<=.4*max(a[2]-a[0],b[2]-b[0])
                  and abs((a[1]+a[3])-(b[1]+b[3]))<=.4*max(a[3]-a[1],b[3]-b[1]))
            stable=(min(area_a,area_b)>0 and ix*iy/min(area_a,area_b)>=.85
                    and min(area_a,area_b)/max(area_a,area_b)>=.60 and near)
            if union<=0 or (ix*iy/union<.65 and not stable):return []
            x1,y1,x2,y2=min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])
            regions.append((x1,y1,x2,y2))
        return regions

    def _split_header_icons(self,header_bytes):
        if not header_bytes:return []
        arr=cv2.imdecode(np.frombuffer(header_bytes,np.uint8),cv2.IMREAD_COLOR)
        if arr is None:return []
        rgb=cv2.cvtColor(arr,cv2.COLOR_BGR2RGB)
        regions=self._validated_header_regions(rgb)
        return [cv2.resize(cv2.cvtColor(rgb[y1:y2,x1:x2],cv2.COLOR_RGB2GRAY),(40,40)) for x1,y1,x2,y2 in regions]

    def _template_similarity(self, tpl, feat):
        """归一化互相关相似度"""
        a = tpl.astype(np.float32) - tpl.mean()
        b = feat.astype(np.float32) - feat.mean()
        denom = (a.std() * b.std() * a.size) + 1e-6
        return float((a * b).sum() / denom)

    # ------------------------------------------------------------------
    # 点选文字验证码
    # ------------------------------------------------------------------

    def solve_click_captcha(self, page):
        if not self.is_available():
            return False
        watch = self._start_verification_watch(page)
        try:
            # 只对 gc-picture 区域做检测, 避免 header/footer 干扰
            bg_loc = page.locator('img.gc-picture').first
            if bg_loc.count() == 0:
                bg_loc = page.locator('div.gc-body img').first
            if bg_loc.count() == 0:
                return False
            bg_bytes = bg_loc.screenshot()
            bg_box = bg_loc.bounding_box()
            if not bg_bytes or not bg_box:
                return False

            boxes = self._ocr.detection(bg_bytes)
            if not boxes:
                return False

            words = self._extract_click_words(page)
            if not words:
                logging.warning("点选验证码未识别出提示文字，放弃本轮以等待新题")
                return False

            bg_img = Image.open(BytesIO(bg_bytes))
            candidates = []
            for box in boxes:
                x1, y1, x2, y2 = box[0], box[1], box[2], box[3]
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue
                crop = bg_img.crop((x1, y1, x2, y2))
                buf = BytesIO()
                crop.save(buf, format='PNG')
                text = self._ocr.classification(buf.getvalue()).strip()
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                candidates.append((cx, cy, text))

            # 按提示文字的顺序点击。每个目标必须匹配成功；缺一个就放弃本轮，
            # 不能像旧逻辑那样把所有检测框依次点一遍，否则会白白消耗验证码次数。
            selected = []
            for word in words:
                matched = None
                # 优先精确匹配，再接受 OCR 返回的短字符串中包含目标字。
                for exact_only in (True, False):
                    for cx, cy, text in candidates:
                        if (cx, cy) in selected:
                            continue
                        normalized = self._normalize_candidate_text(text)
                        if not normalized:
                            continue
                        ok = normalized == word if exact_only else word in normalized
                        if ok:
                            matched = (cx, cy)
                            break
                    if matched:
                        break
                if not matched:
                    logging.warning(
                        "点选验证码目标“%s”未匹配，候选=%s",
                        word, [self._normalize_candidate_text(item[2]) for item in candidates],
                    )
                    return False
                selected.append(matched)

            # 转换为屏幕坐标并点击
            scale_x = bg_box['width'] / bg_img.width
            scale_y = bg_box['height'] / bg_img.height
            for cx, cy in selected:
                page.mouse.click(bg_box['x'] + cx * scale_x, bg_box['y'] + cy * scale_y)
                time.sleep(random.uniform(0.25, 0.45))

            # 点确认按钮
            confirm = page.locator('div.gc-button-block button').first
            if confirm.count() > 0:
                time.sleep(random.uniform(0.2, 0.4))
                confirm.click()

            return self._wait_captcha_closed(page)
        except Exception:
            return False
        finally:
            self._stop_verification_watch(page, watch)

    def _extract_click_hint(self, page):
        hint = self._get_visible_text(page, [
            'div.gc-word', 'div.gc-header', 'div.gc-tips',
            'div.gc-head', 'div.gc-info',
        ])
        return re.sub(r'\s+', ' ', hint).strip() if hint else ''

    def _extract_click_words(self, page):
        """读取点选顺序：优先 DOM 文本，取不到时 OCR 提示小图。"""
        words = self._normalize_words(self._extract_click_hint(page))
        if words:
            return words

        for selector in (
            'div.gc-word img', 'div.gc-word',
            'div.gc-header img', 'img.gc-thumb',
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=300):
                    continue
                image_bytes = locator.screenshot()
                if not image_bytes:
                    continue
                recognized = self._ocr.classification(image_bytes).strip()
                words = self._normalize_words(recognized)
                if words:
                    logging.info("点选验证码提示识别：%s", " ".join(words))
                    return words
            except Exception as exc:
                logging.debug("读取点选验证码提示图失败（%s）: %s", selector, exc)
        return []

    def _normalize_words(self, hint):
        if not hint:
            return []
        text = str(hint)
        # go-captcha 常见提示为“请依次点击春花秋月”，目标汉字之间不一定有空格。
        # 先移除指令用语，再把连续中文拆成单字，保留原始点击顺序。
        text = re.sub(
            r'(?i)please|tap|click|请选择|请依次|依次|按照顺序|按顺序|顺序|点击|选出|完成|验证|文字|字符|下图|图中|请',
            ' ', text,
        )
        tokens = re.findall(r'[\u3400-\u9fff]|[A-Za-z0-9]+', text)
        words = []
        for token in tokens:
            if re.fullmatch(r'[\u3400-\u9fff]', token):
                words.append(token)
            else:
                # 英文/数字点选也按单字符顺序处理；验证码目标通常是单个字形。
                words.extend(list(token.casefold()))
        return words

    @staticmethod
    def _normalize_candidate_text(text):
        chars = re.findall(r'[\u3400-\u9fffA-Za-z0-9]', str(text or ''))
        return ''.join(chars).casefold()

    # ------------------------------------------------------------------
    # 旋转验证码 —— 内/外圈环带相关性最大化
    # ------------------------------------------------------------------

    def solve_rotate_captcha(self, page):
        self.last_decision={'kind':'rotate','accepted':False,'verification_started':False}
        if not self.is_available():
            return False
        watch = self._start_verification_watch(page)
        try:
            outer_bytes = self._capture_element_bytes(page, 'div.gc-rotate-picture img, div.gc-rotate-picture, img.gc-picture')
            inner_bytes = self._capture_element_bytes(page, 'div.gc-rotate-thumb img, div.gc-rotate-thumb, img.gc-thumb')
            angle = None
            if outer_bytes and inner_bytes:
                angle = self._estimate_align_angle(outer_bytes, inner_bytes)
            if angle is None:
                self.last_decision['reason']=self.last_decision.get('rotation_reason','旋转接缝未得到可靠角度，未刷新或提交校验')
                return False
            if self._capture_element_bytes(page, 'div.gc-rotate-picture img, div.gc-rotate-picture, img.gc-picture')!=outer_bytes or self._capture_element_bytes(page, 'div.gc-rotate-thumb img, div.gc-rotate-thumb, img.gc-thumb')!=inner_bytes:
                self.last_decision['reason']='旋转图片已变化，停止旧答案提交';return False
            self.last_decision.update(accepted=True,reason='多个可靠接缝带的角度一致')
            if not self._drag_rotation_slider(page, angle):
                return False
            return self._wait_captcha_closed(page)
        except Exception:
            return False
        finally:
            self._stop_verification_watch(page, watch)

    def _extract_rotation_angle_from_text(self, page):
        raw = self._get_visible_text(page, [
            'div.gc-body', 'div.gc-header', 'div.gc-tips', 'div.gc-wrapper',
        ])
        if not raw:
            return None
        m = re.search(r'(\d{1,3})\s*[°度]', raw)
        return int(m.group(1)) if m else None

    def _estimate_align_angle(self, outer_bytes, inner_bytes):
        """Cross-check independent seam bands with normalized color correlation."""
        outer = cv2.imdecode(np.frombuffer(outer_bytes, np.uint8), cv2.IMREAD_COLOR)
        inner = cv2.imdecode(np.frombuffer(inner_bytes, np.uint8), cv2.IMREAD_COLOR)
        if outer is None or inner is None:
            return None
        oh, ow = outer.shape[:2]
        ih, iw = inner.shape[:2]
        if min(oh, ow, ih, iw) < 30 or abs(oh - ow) > 2 or abs(ih - iw) > 2:
            return None
        radius = min(ih, iw) / 2
        scale = radius / 80
        if radius + 8 * scale >= min(oh, ow) / 2 - 1:
            return None
        theta = np.arange(720) * 2 * np.pi / 720

        def polar(image, offsets):
            h, w = image.shape[:2]
            radii = (radius + np.asarray(offsets) * scale)[:, None]
            x = (w / 2 + radii * np.cos(theta)[None, :]).astype('float32')
            y = (h / 2 + radii * np.sin(theta)[None, :]).astype('float32')
            return cv2.remap(image.astype('float32'), x, y, cv2.INTER_LINEAR).mean(0)

        bands = [([-5, -4, -3], [2, 3, 4]), ([-8, -6, -4], [4, 6, 8]), ([-3, -2], [1, 2])]
        measurements = []
        reliable = []
        for inside, outside in bands:
            a = polar(outer, outside)
            b = polar(inner, inside)
            av = float(a.var(axis=0).mean())
            bv = float(b.var(axis=0).mean())
            if min(av, bv) < 50:
                continue
            centered_a = a - a.mean(axis=0)
            centered_b = b - b.mean(axis=0)
            denominator = np.sqrt(np.square(centered_a).sum() * np.square(centered_b).sum())
            correlation = np.asarray([
                (centered_a * np.roll(centered_b, step, axis=0)).sum() / denominator
                for step in range(720)
            ])
            if not np.isfinite(correlation).all():
                continue
            best = int(correlation.argmax())
            distance = np.minimum((np.arange(720) - best) % 720, (best - np.arange(720)) % 720)
            peak = float(correlation[best])
            margin = peak - float(correlation[distance > 30].max())
            residual = float(np.square(a - np.roll(b, best, axis=0)).mean()) / (av + bv)
            row = {'angle': best / 2, 'correlation': round(peak, 4),
                   'margin': round(margin, 4), 'residual': round(residual, 4)}
            measurements.append(row)
            if peak >= .65 and margin >= .20 and residual <= .35:
                reliable.append(row)
        self.last_decision['seam_bands'] = measurements
        if len(reliable) < 2:
            self.last_decision['rotation_reason'] = '接缝纹理不足或候选角度存在歧义'
            return None
        reference = reliable[0]['angle']
        angles = [reference + (row['angle'] - reference + 180) % 360 - 180 for row in reliable]
        if max(angles) - min(angles) > 5:
            self.last_decision['rotation_reason'] = '可靠接缝带的角度不一致'
            return None
        degree = float(np.median(angles) % 360)
        self.last_decision.update(method='normalized_seam_consensus', angle=degree,
                                  ring_angles=[row['angle'] for row in reliable])
        return degree

    def _multi_ring_score(self, outer, inner, cx, cy, radius):
        """多环带归一化互相关评分"""
        score = 0.0
        for r_ratio in (0.7, 0.8, 0.9, 0.95):
            r = int(radius * r_ratio)
            n = max(36, int(r * 2))
            outer_vals = []
            inner_vals = []
            for i in range(n):
                a = 2 * math.pi * i / n
                x = int(cx + r * math.cos(a))
                y = int(cy + r * math.sin(a))
                if 0 <= x < outer.shape[1] and 0 <= y < outer.shape[0]:
                    outer_vals.append(float(outer[y, x]))
                    inner_vals.append(float(inner[y, x]))
            if len(outer_vals) < 10:
                continue
            a = np.array(outer_vals) - np.mean(outer_vals)
            b = np.array(inner_vals) - np.mean(inner_vals)
            denom = (np.std(a) * np.std(b) * len(a)) + 1e-6
            score += float(np.dot(a, b) / denom)
        return score

    def _sample_ring(self, img, cx, cy, r_inner, r_outer, n_angles=180):
        vals = []
        for r in range(r_inner, r_outer + 1):
            for i in range(n_angles):
                a = 2 * math.pi * i / n_angles
                x = int(cx + r * math.cos(a))
                y = int(cy + r * math.sin(a))
                if 0 <= x < img.shape[1] and 0 <= y < img.shape[0]:
                    vals.append(float(img[y, x]))
                else:
                    vals.append(0.0)
        if not vals:
            return None
        return np.array(vals, dtype=np.float32)

    def _drag_rotation_slider(self, page, degree):
        try:
            track = page.locator('div.gc-drag-slide-bar').first
            handle = page.locator('div.gc-drag-block').first
            track_box = track.bounding_box()
            handle_box = handle.bounding_box()
            if not handle_box or not track_box:
                return False

            if not math.isfinite(float(degree)) or not 0<=float(degree)<360:return False
            if not all(math.isfinite(float(box[k])) for box in (track_box,handle_box) for k in ('x','y','width','height')):return False
            ratio = float(degree) / 360.0
            usable_w = track_box['width'] - handle_box['width']
            if usable_w<=0 or min(track_box['height'],handle_box['width'],handle_box['height'])<=0:return False
            target_x = track_box['x'] + handle_box['width'] / 2 + ratio * usable_w
            start_x = handle_box['x'] + handle_box['width'] / 2
            start_y = handle_box['y'] + handle_box['height'] / 2
            self.last_decision['verification_started']=True
            return self._drag_by_track(page, start_x, start_y, target_x, start_y)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 拖拽执行
    # ------------------------------------------------------------------

    def _drag_by_track(self, page, start_x, start_y, end_x, end_y):
        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.08, 0.16))
        page.mouse.down()
        track = generate_slide_track(end_x - start_x)
        for pt in track:
            page.mouse.move(start_x + pt['x'], end_y + pt['y'], steps=2)
            time.sleep(pt['t'] / 1000.0)
        page.mouse.up()
        time.sleep(random.uniform(0.3, 0.6))
        return True

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _capture_element_bytes(self, page, selector):
        try:
            for sel in selector.split(','):
                sel = sel.strip()
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first.screenshot()
        except Exception:
            pass
        return None
