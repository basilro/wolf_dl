import threading
import traceback

from .model import ModelWolfItem
from .setup import *
from .worker import Worker


def _merge_titles(existing: str, new_token: str) -> tuple:
    """titles 설정(줄바꿈 구분)에 new_token 추가. 중복이면 변경 없음.

    반환: (병합된 문자열, 추가되었는지 bool)
    """
    lines = [l.strip() for l in (existing or '').replace('\r', '').split('\n')]
    lines = [l for l in lines if l]
    tok = (new_token or '').strip()
    if not tok:
        return (existing or '', False)
    # work_id 기준 중복 검사
    from .client import WolfClient
    parsed_new = WolfClient.extract_work_id(tok)
    new_id = parsed_new[1] if parsed_new else None
    for l in lines:
        if l == tok:
            return ('\n'.join(lines), False)
        if new_id:
            p = WolfClient.extract_work_id(l)
            if p and p[1] == new_id:
                return ('\n'.join(lines), False)
    lines.append(tok)
    return ('\n'.join(lines), True)


class ModuleBasic(PluginModuleBase):

    def __init__(self, P):
        super(ModuleBasic, self).__init__(
            P, name='basic', first_menu='setting',
            scheduler_desc='늑대닷컴 자동 다운로드',
        )
        self.db_default = {
            f'db_version': '1',
            f'{self.name}_auto_start': 'False',
            # 6시간마다 — 회차 업데이트 빈도가 작품마다 달라 보수적
            f'{self.name}_interval': '0 */6 * * *',
            f'{self.name}_db_delete_day': '90',
            f'{self.name}_db_auto_delete': 'False',
            f'{P.package_name}_item_last_list_option': '',

            # 작품 목록 (만화 단일) — toon URL/ID 줄바꿈 구분
            'titles': '',

            # 사이트 설정
            'base_url': 'https://wfwf436.com',
            'download_path': '',
            'max_per_run': '5',           # 작품당 1회 실행 최대 다운 회차
            'use_compress': 'False',      # 회차 폴더 ZIP 압축

            # 인증 (쿠키 + 프록시 + FlareSolverr + 도메인 자동 갱신)
            'cookies': '',
            'use_proxy': 'False',
            'proxy_url': '',
            'flaresolverr_url': '',       # Cloudflare 우회용
            'auto_resolve_base_url': 'True',
            # 도메인 공지 — 텔레그램 공개 채널(주소가 자주 바뀜)
            'announcer_url': 'https://t.me/s/wfwf_com',

            # 알림
            'notify_webhook_download': '',    # 다운 완료 알림
            'notify_webhook_alert': '',       # 도메인/쿠키 만료 알림

            'auto_start': 'False',
        }
        self.web_list_model = ModelWolfItem

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_name())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_name())
        return render_template(
            f'{P.package_name}_{self.name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1=None, arg2=None, arg3=None, req=None):
        try:
            P.logger.info('[basic.process_command] cmd=%r arg1=%r arg2=%r arg3=%r',
                          command, arg1, arg2, arg3)
        except Exception:
            pass
        ret = {'ret': 'success'}
        try:
            if command == 'run_now':
                ret = self.do_action()
            elif command == 'sync_metadata':
                ret = self.do_action_sync_metadata()
            elif command == 'compress_all':
                ret = self.do_action_compress_all()
            elif command == 'resolve_base':
                from .client import WolfClient
                proxy_url = WolfClient.resolve_proxy(
                    P.ModelSetting.get('use_proxy'),
                    P.ModelSetting.get('proxy_url'))
                cookies = (P.ModelSetting.get('cookies') or '').strip() or None
                fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
                cur = (P.ModelSetting.get('base_url') or '').strip()
                new_url = WolfClient.resolve_base_url(
                    current_base_url=cur, proxy_url=proxy_url,
                    cookies=cookies, flaresolverr_url=fs_url,
                    logger=P.logger)
                # 숫자 증가 실패 시 announcer(텔레그램/웹) 공지에서 추출
                src = ''
                if not new_url:
                    announcer = (P.ModelSetting.get('announcer_url') or '').strip()
                    if announcer:
                        try:
                            from .announcer import resolve_from_announcer
                            new_url = resolve_from_announcer(
                                announcer, proxy_url=proxy_url, cookies=cookies,
                                flaresolverr_url=fs_url, logger=P.logger)
                            if new_url:
                                src = ' (공지 채널)'
                        except Exception as e:
                            P.logger.warning('announcer 갱신 예외: %s', e)
                if new_url:
                    if cur != new_url:
                        P.ModelSetting.set('base_url', new_url)
                        ret = {'ret': 'success', 'base_url': new_url,
                               'msg': f'도메인 갱신됨{src}: {cur} → {new_url}'}
                    else:
                        ret = {'ret': 'success', 'base_url': new_url,
                               'msg': f'현재 도메인 유효: {new_url}'}
                else:
                    announcer = (P.ModelSetting.get('announcer_url')
                                 or '').strip()
                    hint = (f' — 공지 채널 확인: {announcer}'
                            if announcer else '')
                    ret = {'ret': 'fail',
                           'msg': f'자동 갱신 후보 모두 실패{hint}'}
            elif command == 'ping_base':
                from .client import WolfClient
                proxy_url = WolfClient.resolve_proxy(
                    P.ModelSetting.get('use_proxy'),
                    P.ModelSetting.get('proxy_url'))
                base = (P.ModelSetting.get('base_url') or '').strip() or None
                cookies = (P.ModelSetting.get('cookies') or '').strip() or None
                fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
                try:
                    cli = WolfClient(base_url=base, logger=P.logger,
                                     proxy_url=proxy_url, cookies=cookies,
                                     flaresolverr_url=fs_url)
                    h = cli.check_health()
                    if h['domain_ok'] and h.get('cookies_ok') is not False:
                        ret = {'ret': 'success',
                               'msg': f'접속 OK — {cli.base_url}'}
                    elif h['domain_ok'] and h.get('cookies_ok') is False:
                        ret = {'ret': 'fail',
                               'msg': f'쿠키 만료 의심 — {h["reason"]}'}
                    else:
                        ret = {'ret': 'fail',
                               'msg': f'도메인 실패 — {h["reason"]} ({cli.base_url})'}
                except Exception as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'check_announcer':
                # 텔레그램/웹 공지에서 도메인 후보만 확인 (적용 안 함)
                from .client import WolfClient
                from .announcer import fetch_announced_domains
                proxy_url = WolfClient.resolve_proxy(
                    P.ModelSetting.get('use_proxy'),
                    P.ModelSetting.get('proxy_url'))
                announcer = (P.ModelSetting.get('announcer_url') or '').strip()
                if not announcer:
                    ret = {'ret': 'fail', 'msg': '공지 URL 미설정'}
                else:
                    try:
                        cands = fetch_announced_domains(
                            announcer, proxy_url=proxy_url, logger=P.logger)
                        if cands:
                            ret = {'ret': 'success', 'candidates': cands,
                                   'msg': '공지 도메인: ' + ', '.join(cands)}
                        else:
                            ret = {'ret': 'fail',
                                   'msg': '공지에서 도메인 추출 실패'}
                    except Exception as e:
                        ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'msearch':
                # 제목 검색 → 작품 카드 리스트
                from .client import WolfClient
                q = (arg1 or '').strip()
                if not q and req is not None:
                    try:
                        q = (req.form.get('query') or req.values.get('query')
                             or '').strip()
                    except Exception:
                        pass
                if not q:
                    ret = {'ret': 'fail', 'msg': '검색어 없음'}
                else:
                    proxy_url = WolfClient.resolve_proxy(
                        P.ModelSetting.get('use_proxy'),
                        P.ModelSetting.get('proxy_url'))
                    base = (P.ModelSetting.get('base_url') or '').strip() or None
                    cookies = (P.ModelSetting.get('cookies') or '').strip() or None
                    fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
                    try:
                        cli = WolfClient(base_url=base, logger=P.logger,
                                         proxy_url=proxy_url, cookies=cookies,
                                         flaresolverr_url=fs_url)
                        results = cli.search(q)
                        ret = {'ret': 'success', 'results': results,
                               'count': len(results)}
                    except Exception as e:
                        ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'mbrowse':
                # 연재/완결 목록 + 요일·성인·장르·정렬 필터
                from .client import WolfClient

                def _f(key, default=''):
                    v = None
                    if req is not None:
                        try:
                            v = (req.form.get(key) or req.values.get(key))
                        except Exception:
                            v = None
                    return (v if v is not None else default)

                status = (arg1 or _f('status', 'ing')).strip() or 'ing'
                t1 = _f('t1', '').strip()      # 요일
                t2 = _f('t2', '').strip()      # 구분(일반/BL/성인)
                t3 = _f('t3', '').strip()      # 장르
                o = (_f('o', 'n').strip() or 'n')  # 정렬
                try:
                    pg = int((arg2 or _f('pg', '1')).strip() or '1')
                except Exception:
                    pg = 1
                proxy_url = WolfClient.resolve_proxy(
                    P.ModelSetting.get('use_proxy'),
                    P.ModelSetting.get('proxy_url'))
                base = (P.ModelSetting.get('base_url') or '').strip() or None
                cookies = (P.ModelSetting.get('cookies') or '').strip() or None
                fs_url = (P.ModelSetting.get('flaresolverr_url') or '').strip() or None
                try:
                    cli = WolfClient(base_url=base, logger=P.logger,
                                     proxy_url=proxy_url, cookies=cookies,
                                     flaresolverr_url=fs_url)
                    data = cli.browse(status=status, t1=t1, t2=t2, t3=t3,
                                      o=o, pg=pg)
                    ret = {'ret': 'success',
                           'results': data['cards'], 'count': len(data['cards']),
                           'last_page': data['last_page'], 'page': data['page'],
                           'status': data['status']}
                except Exception as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'add_title':
                # 수동 다운로드 → 자동 등록 작품 목록에 추가
                tok = (arg1 or '').strip()
                if not tok and req is not None:
                    try:
                        tok = (req.form.get('url') or req.values.get('url')
                               or '').strip()
                    except Exception:
                        pass
                if not tok:
                    ret = {'ret': 'fail', 'msg': '추가할 작품 URL/ID 없음'}
                else:
                    cur = P.ModelSetting.get('titles') or ''
                    merged, added = _merge_titles(cur, tok)
                    if added:
                        P.ModelSetting.set('titles', merged)
                        ret = {'ret': 'success',
                               'msg': f'목록에 추가됨: {tok}'}
                    else:
                        ret = {'ret': 'success',
                               'msg': f'이미 목록에 있음: {tok}'}
            elif command == 'mrun':
                from . import manual_worker
                url = (arg1 or '').strip()
                if not url and req is not None:
                    try:
                        url = (req.form.get('url') or req.values.get('url')
                               or req.args.get('url') or '').strip()
                    except Exception:
                        pass
                ret = manual_worker.run_with_url(url)
            elif command == 'mcancel':
                from . import manual_worker
                manual_worker.cancel()
                ret = {'ret': 'success', 'msg': '취소 요청 보냄'}
            elif command == 'mprogress':
                from . import manual_worker
                ret = {'ret': 'success', 'state': manual_worker.get_state()}
            elif command == 'status_progress':
                from . import manual_worker, worker as auto_worker
                ret = {
                    'ret': 'success',
                    'auto': auto_worker.get_auto_state(),
                    'manual': manual_worker.get_state(),
                }
            elif command == 'notify_test':
                # arg1 = 'download' | 'alert'
                from .notify import send_webhook
                kind = (arg1 or 'download').strip().lower()
                if kind == 'alert':
                    url_key = 'notify_webhook_alert'
                    label = '도메인/쿠키 알림'
                else:
                    url_key = 'notify_webhook_download'
                    label = '다운로드'
                url = (P.ModelSetting.get(url_key) or '').strip()
                if not url:
                    ret = {'ret': 'fail', 'msg': f'{kind} URL 미설정'}
                else:
                    msg = f'[늑대닷컴 {label}] 테스트 알림 — 정상 수신 확인용'
                    ok = send_webhook(url, msg)
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '발송 성공' if ok else '발송 실패 (URL/형식 확인)'}
            elif command == 'db_delete_items':
                ids = []
                for x in (arg1 or '').split(','):
                    x = x.strip()
                    if x.isdigit():
                        ids.append(int(x))
                if not ids:
                    ret = {'ret': 'fail', 'msg': '삭제할 ID 없음', 'count': 0}
                else:
                    cnt = (db.session.query(ModelWolfItem)
                           .filter(ModelWolfItem.id.in_(ids))
                           .delete(synchronize_session=False))
                    db.session.commit()
                    ret = {'ret': 'success', 'count': cnt}
        except Exception as e:
            P.logger.error('[basic.process_command] inner Exception: %s', e)
            P.logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        try:
            return jsonify(ret)
        except Exception as e:
            P.logger.error('[basic.process_command] jsonify 실패: %s ret=%r', e, ret)
            return jsonify({'ret': 'fail', 'msg': f'jsonify 실패: {e}'})

    def scheduler_function(self):
        P.logger.info('[basic] scheduler_function CALLED')
        try:
            ret = self.do_action()
            P.logger.info('[basic] scheduler 종료: %s', ret)
        except Exception as e:
            P.logger.error('[basic] scheduler Exception: %s', e)
            P.logger.error(traceback.format_exc())

    def do_action(self):
        P.logger.info('[basic] do_action BEGIN')
        try:
            with F.app.app_context():
                w = Worker()
                ret = w.run()
                P.logger.info('[basic] do_action END ret=%s', ret)
                return ret
        except Exception as e:
            P.logger.error('[basic] do_action Exception: %s', e)
            P.logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}

    def do_action_sync_metadata(self):
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 자동 다운로드 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().sync_metadata_all()
            except Exception as e:
                P.logger.error('[basic] sync_metadata Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '메타 동기화 시작됨 — "진행 상황" 메뉴에서 확인'}

    def do_action_compress_all(self):
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 다른 작업 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().compress_all()
            except Exception as e:
                P.logger.error('[basic] compress_all Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '압축 시작됨 — "진행 상황" 메뉴에서 확인'}
