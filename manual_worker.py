"""수동 다운로드 워커 — 작품 URL/ID 하나의 회차 전체 직렬 다운로드."""
import os
import re
import threading
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List

from .client import (WolfClient, WolfError, NotReadableError,
                     BlockedError, KIND_LABEL, DEFAULT_KIND)
from .model import ModelWolfItem
from .setup import *  # P, db, logger
from . import worker as _wkr  # ensure_title_metadata / compress_episode_folder


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    'status': 'idle',           # idle | analyzing | running | done | error | canceled
    'message': '',
    'kind': '',
    'work_id': None,
    'work_title': '',
    'started_at': None,
    'finished_at': None,
    'episodes': [],             # [{no,title,ep_url_id,paid,state,pages_done,pages_total,save_dir,error}]
    'current_index': -1,
    'total_to_download': 0,
    'completed': 0,
    'skipped': 0,
    'failed': 0,
    # 내부 — UI 비노출
    '_meta': None,
}
_cancel_flag = threading.Event()
_thread: Optional[threading.Thread] = None


def get_state() -> Dict[str, Any]:
    with _state_lock:
        snap = {k: v for k, v in _state.items()
                if k != 'episodes' and not k.startswith('_')}
        snap['episodes'] = [dict(e) for e in _state['episodes']]
        return snap


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def _reset_state():
    with _state_lock:
        _state.update({
            'status': 'idle', 'message': '',
            'kind': '', 'work_id': None, 'work_title': '',
            'started_at': None, 'finished_at': None,
            'episodes': [], 'current_index': -1,
            'total_to_download': 0,
            'completed': 0, 'skipped': 0, 'failed': 0,
            '_meta': None,
        })


def is_running() -> bool:
    with _state_lock:
        return _state['status'] in ('analyzing', 'running')


def cancel():
    _cancel_flag.set()
    _set(message='취소 요청됨')


def _build_client() -> WolfClient:
    base = (P.ModelSetting.get('base_url') or '').strip() or None
    proxy_url = WolfClient.resolve_proxy(
        P.ModelSetting.get('use_proxy'),
        P.ModelSetting.get('proxy_url'))
    cookies = (P.ModelSetting.get('cookies') or '').strip() or None
    fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
    return WolfClient(base_url=base, logger=P.logger,
                      proxy_url=proxy_url, cookies=cookies,
                      flaresolverr_url=fs_url)


def analyze(url_or_id: str, default_kind: str = DEFAULT_KIND) -> Dict[str, Any]:
    """URL → 작품 메타 + 회차 목록. 다운로드는 안 함."""
    P.logger.info('[manual] analyze BEGIN url=%r', url_or_id)
    parsed = WolfClient.extract_work_id(url_or_id)
    if not parsed:
        return {'ret': 'fail', 'msg': f'URL에서 workId 추출 실패: {url_or_id!r}'}
    kind, work_id = parsed

    try:
        cli = _build_client()
    except Exception as e:
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'클라이언트 생성 실패: {e}'}

    try:
        meta = cli.get_work(kind, work_id)
    except BlockedError as e:
        return {'ret': 'fail', 'msg': f'서버 차단 (재시도 권장): {e}'}
    except WolfError as e:
        return {'ret': 'fail', 'msg': f'작품 조회 실패: {e}'}

    work_title = (meta.get('title') or '').strip() or f'만화_{work_id}'

    all_eps: List[Dict[str, Any]] = []
    for ep in meta.get('episodes') or []:
        all_eps.append({
            'no': int(ep.get('no') or 0),
            'title': ep.get('title') or '',
            'ep_url_id': ep.get('ep_url_id') or '',
            'paid': bool(ep.get('paid')),
            'state': 'pending',
            'pages_done': 0,
            'pages_total': 0,
            'save_dir': '',
            'error': '',
        })
    # 다운 가능 후보: 무료
    episodes = [e for e in all_eps if not e['paid']]
    episodes.sort(key=lambda e: e['no'])
    will_download = len(episodes)

    _reset_state()
    _set(status='idle',
         message=(f'분석 완료 — 전체 {len(all_eps)}개 중 '
                  f'다운로드 가능 {will_download}개'),
         kind=kind, work_id=work_id, work_title=work_title,
         episodes=episodes, total_to_download=will_download,
         _meta=meta)
    P.logger.info('[manual] analyze END work=%r total=%d free=%d',
                  work_title, len(all_eps), will_download)
    return {
        'ret': 'success',
        'kind': kind, 'kind_label': KIND_LABEL.get(kind, kind),
        'work_id': work_id, 'work_title': work_title,
        'episodes': episodes,
        'will_download': will_download,
        'total': len(all_eps),
    }


def run_with_url(url_or_id: str,
                 default_kind: str = DEFAULT_KIND) -> Dict[str, Any]:
    P.logger.info('[manual] run_with_url BEGIN url=%r', url_or_id)
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    ar = analyze(url_or_id, default_kind=default_kind)
    if ar.get('ret') != 'success':
        return ar
    sr = start()
    return {
        'ret': sr.get('ret', 'fail'),
        'msg': sr.get('msg', ''),
        'kind': ar.get('kind'), 'kind_label': ar.get('kind_label'),
        'work_id': ar.get('work_id'),
        'work_title': ar.get('work_title'),
        'will_download': ar.get('will_download'),
        'total': ar.get('total'),
    }


def start() -> Dict[str, Any]:
    global _thread
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    with _state_lock:
        if not _state['work_id'] or not _state['episodes']:
            return {'ret': 'fail', 'msg': '먼저 작품을 분석하세요'}
    download_root = (P.ModelSetting.get('download_path') or '').strip()
    if not download_root:
        return {'ret': 'fail', 'msg': 'download_path 미설정'}

    _cancel_flag.clear()
    _set(status='running', message='다운로드 시작',
         started_at=datetime.now().isoformat(),
         finished_at=None, current_index=-1,
         completed=0, skipped=0, failed=0)
    _thread = threading.Thread(target=_run, args=(download_root,), daemon=True)
    _thread.start()
    return {'ret': 'success', 'msg': '시작됨'}


def _run(download_root: str):
    with F.app.app_context():
        try:
            cli = _build_client()
            with _state_lock:
                kind = _state['kind']
                work_id = _state['work_id']
                work_title = _state['work_title']
                episodes = list(_state['episodes'])
                meta = _state.get('_meta') or {}

            # info.xml / cover.jpg — 첫 다운로드 전에 생성
            try:
                _wkr.ensure_title_metadata(cli, download_root, meta)
            except Exception as e:
                P.logger.warning('[manual] ensure_title_metadata 실패: %s', e)

            for idx, ep in enumerate(episodes):
                if _cancel_flag.is_set():
                    _set(status='canceled',
                         finished_at=datetime.now().isoformat(),
                         message='취소됨')
                    return
                _set(current_index=idx)
                P.logger.info('[manual] [%d/%d] %s (no=%s ep=%s)',
                              idx + 1, len(episodes),
                              ep.get('title'), ep.get('no'),
                              ep.get('ep_url_id'))
                ok = _download_episode(cli, kind, work_id, work_title,
                                       idx, ep, download_root)
                with _state_lock:
                    if ok == 'completed':
                        _state['completed'] += 1
                    elif ok == 'skipped':
                        _state['skipped'] += 1
                    else:
                        _state['failed'] += 1

            _set(status='done', finished_at=datetime.now().isoformat(),
                 current_index=-1, message='완료')
        except BlockedError as e:
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'서버 차단: {e}')
        except Exception as e:
            P.logger.error('[manual] _run exception: %s', e)
            P.logger.error(traceback.format_exc())
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'에러: {e}')


def _ep_update(idx: int, **kw):
    with _state_lock:
        _state['episodes'][idx].update(kw)


def _download_episode(cli: WolfClient, kind: str, work_id: str,
                      work_title: str, idx: int, ep: Dict[str, Any],
                      download_root: str) -> str:
    no = int(ep.get('no') or 0)
    ep_title = ep.get('title') or f'{no}화'
    ep_url_id = ep.get('ep_url_id') or ''

    rec = (db.session.query(ModelWolfItem)
           .filter_by(work_kind=kind, work_id=work_id,
                      ep_url_id=ep_url_id).first())
    if rec and rec.status == 'completed':
        _ep_update(idx, state='completed', save_dir=rec.save_dir or '',
                   pages_done=rec.downloaded_count or 0,
                   pages_total=rec.page_count or 0)
        return 'completed'
    if rec is None:
        rec = ModelWolfItem()
        rec.work_kind = kind
        rec.work_id = work_id
        rec.work_title = work_title
        rec.ep_url_id = ep_url_id
        rec.episode_no = no
        rec.episode_title = ep_title
        db.session.add(rec)
        db.session.commit()

    _ep_update(idx, state='downloading', error='')
    rec.status = 'downloading'; rec.updated_time = datetime.now()
    db.session.commit()

    return _download_image_one(cli, rec, kind, work_id, work_title, idx,
                               ep_url_id, no, ep_title, download_root)


def _download_image_one(cli, rec, kind, work_id, work_title, idx,
                        ep_url_id, no, ep_title, download_root) -> str:
    try:
        urls, parsed_subtitle = cli.get_episode_images(
            kind, work_id, ep_url_id)
    except NotReadableError as e:
        _ep_update(idx, state='skipped', error=f'잠금: {e}')
        rec.status = 'skipped_paid'; rec.error_msg = str(e)
        db.session.commit()
        return 'skipped'
    except BlockedError as e:
        _ep_update(idx, state='failed', error=f'blocked: {e}')
        rec.status = 'failed'; rec.error_msg = f'blocked: {e}'
        db.session.commit()
        return 'failed'
    except WolfError as e:
        _ep_update(idx, state='failed', error=f'images: {e}')
        rec.status = 'failed'; rec.error_msg = f'images: {e}'
        db.session.commit()
        return 'failed'

    if not ep_title.strip() and parsed_subtitle:
        ep_title = parsed_subtitle
        rec.episode_title = ep_title

    save_dir = os.path.join(
        _wkr.title_dir_for(download_root, kind, work_title),
        f'{no:04d}_{_safe_filename(ep_title)}')
    os.makedirs(save_dir, exist_ok=True)
    rec.save_dir = save_dir
    rec.page_count = len(urls)
    db.session.commit()
    _ep_update(idx, save_dir=save_dir, pages_total=len(urls), pages_done=0)

    referer = cli.home_referer()
    downloaded = 0; total_bytes = 0; failed = 0
    for i, url in enumerate(urls, start=1):
        if _cancel_flag.is_set():
            break
        try:
            data = cli.download_image(url, referer=referer)
            ext = WolfClient.url_ext(url)
            local = os.path.join(save_dir, f'{i:03d}{ext}')
            with open(local, 'wb') as fp:
                fp.write(data)
            total_bytes += len(data)
            downloaded += 1
            _ep_update(idx, pages_done=downloaded)
        except Exception as e:
            failed += 1
            P.logger.warning('[manual] %s p%d 실패: %s', ep_title, i, e)

    rec.downloaded_count = downloaded
    rec.total_bytes = total_bytes
    rec.downloaded_at = datetime.now()
    rec.updated_time = rec.downloaded_at
    if downloaded == len(urls):
        rec.status = 'completed'
        _ep_update(idx, state='completed')
        db.session.commit()
        if (P.ModelSetting.get('use_compress') or 'False') == 'True':
            zip_path = _wkr.compress_episode_folder(save_dir)
            if zip_path:
                rec.save_dir = zip_path
                db.session.commit()
                _ep_update(idx, save_dir=zip_path)
                P.logger.info('[manual] %s 압축 완료 → %s',
                              ep_title, zip_path)
        return 'completed'
    elif downloaded > 0:
        rec.status = 'partial'
        rec.error_msg = f'failed {failed}/{len(urls)}'
        _ep_update(idx, state='failed', error=f'부분실패 {failed}/{len(urls)}')
        db.session.commit()
        return 'failed'
    else:
        rec.status = 'failed'
        rec.error_msg = f'all failed ({len(urls)})'
        _ep_update(idx, state='failed', error='전부 실패')
        db.session.commit()
        return 'failed'
