"""도메인 공지 파서 — 텔레그램 공개 채널 / 일반 웹페이지에서 최신 주소 추출.

늑대닷컴은 주소가 자주 바뀌며 공식 텔레그램 채널로 공지된다.
공개 채널이면 로그인/봇 없이 `https://t.me/s/<채널명>` 웹 프리뷰를 그대로
GET 해서 최신 메시지의 도메인만 정규식으로 뽑아올 수 있다.

핀 메시지 예시:
    늑대닷컴 주소
    https://wfwf436.com

    늑대닷컴2 주소
    https://wftoon223.com

→ '늑대닷컴 주소' 라벨 다음 URL(메인)만 취하고, '늑대닷컴2'(구버전)는 제외.
"""
import html as html_lib
import re
from typing import List, Optional
from urllib.parse import urlparse

import requests

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36')

# 메인 주소 라벨 — '늑대닷컴2'(뒤에 숫자)는 매칭 제외
_MAIN_LABEL_RE = re.compile(
    r'늑대닷컴(?!\d)\s*(?:주소|바로가기)?\s*[:\-]?\s*'
    r'(https?://[a-zA-Z0-9][a-zA-Z0-9.\-]+\.[a-z]{2,}[^\s<"\']*)')
# 구버전(늑대닷컴2/리뉴얼 전) 라벨 — 이 근처 URL 은 제외
_SUB_LABEL_RE = re.compile(r'늑대닷컴\d')

_URL_RE = re.compile(
    r'https?://[a-zA-Z0-9][a-zA-Z0-9.\-]+\.[a-z]{2,}[^\s<"\']*')


def _norm_base(u: str) -> str:
    """URL → scheme://host (경로/쿼리 제거)."""
    p = urlparse(u.strip())
    if not p.scheme or not p.netloc:
        return ''
    return f'{p.scheme}://{p.netloc}'


def _telegram_preview_url(announcer_url: str) -> Optional[str]:
    """t.me URL 을 웹 프리뷰(`/s/`) 형태로 정규화. 텔레그램 아니면 None."""
    p = urlparse(announcer_url.strip())
    host = (p.netloc or '').lower()
    if host not in ('t.me', 'telegram.me', 'www.t.me'):
        return None
    path = p.path.strip('/')
    # 이미 /s/<channel> 형태면 그대로, /<channel> 이면 /s/ 삽입
    if path.startswith('s/'):
        return f'https://t.me/{path}'
    channel = path.split('/')[0]
    if not channel:
        return None
    return f'https://t.me/s/{channel}'


def _extract_domains_from_text(text: str) -> List[str]:
    """공지 텍스트 1개 → 메인 도메인 후보 (라벨 우선, 없으면 일반 URL)."""
    out: List[str] = []

    # 1) '늑대닷컴 주소' 라벨 다음 URL (메인) 우선
    for m in _MAIN_LABEL_RE.finditer(text):
        b = _norm_base(m.group(1))
        if b and b not in out:
            out.append(b)

    # 2) 라벨 매칭이 없으면 일반 URL 폴백 — t.me / 구버전 라벨 근처는 제외
    if not out:
        for m in _URL_RE.finditer(text):
            u = m.group(0)
            b = _norm_base(u)
            if not b or 't.me' in b or 'telegram' in b:
                continue
            # 앞쪽 40자에 '늑대닷컴2' 등 구버전 라벨이 있으면 스킵
            ctx = text[max(0, m.start() - 40):m.start()]
            if _SUB_LABEL_RE.search(ctx):
                continue
            if b not in out:
                out.append(b)
    return out


def _telegram_messages(html: str) -> List[str]:
    """t.me/s/ HTML → 메시지 텍스트 리스트 (시간순: 앞=과거, 뒤=최신)."""
    msgs: List[str] = []
    for m in re.finditer(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL):
        block = m.group(1)
        block = re.sub(r'<br\s*/?>', '\n', block)
        block = re.sub(r'</?a[^>]*>', ' ', block)
        block = re.sub(r'<[^>]+>', '', block)
        txt = html_lib.unescape(block).strip()
        if txt:
            msgs.append(txt)
    return msgs


def fetch_announced_domains(announcer_url: str,
                            proxy_url: Optional[str] = None,
                            timeout: int = 15,
                            logger=None) -> List[str]:
    """공지 소스에서 메인 도메인 후보 리스트 반환 (최신 우선).

    텔레그램 공개 채널(t.me) 이면 최신 메시지부터, 일반 웹페이지면 본문 전체에서 추출.
    """
    announcer_url = (announcer_url or '').strip()
    if not announcer_url:
        return []
    proxies = None
    if proxy_url:
        proxies = {'http': proxy_url, 'https': proxy_url}

    tg = _telegram_preview_url(announcer_url)
    fetch_url = tg or announcer_url
    try:
        r = requests.get(fetch_url, timeout=timeout,
                         headers={'User-Agent': _UA,
                                  'Accept-Language': 'ko-KR,ko;q=0.9'},
                         proxies=proxies)
    except Exception as e:
        if logger:
            logger.warning('announcer 접속 실패 %s: %s', fetch_url, e)
        return []
    if r.status_code != 200:
        if logger:
            logger.warning('announcer HTTP %s: %s', r.status_code, fetch_url)
        return []
    html = r.text or ''

    out: List[str] = []
    if tg:
        # 최신 메시지부터 (리스트 뒤쪽이 최신)
        for msg in reversed(_telegram_messages(html)):
            for b in _extract_domains_from_text(msg):
                if b not in out:
                    out.append(b)
    else:
        for b in _extract_domains_from_text(html):
            if b not in out:
                out.append(b)

    if logger:
        logger.info('announcer 도메인 후보: %s', ', '.join(out) or '(없음)')
    return out


def resolve_from_announcer(announcer_url: str,
                           proxy_url: Optional[str] = None,
                           cookies: Optional[str] = None,
                           flaresolverr_url: Optional[str] = None,
                           logger=None) -> Optional[str]:
    """공지에서 도메인 후보를 받아 최초로 살아있는(health OK) 주소 반환.

    검증 실패해도 후보가 하나뿐이면 그 후보를 반환(그물망) — 완전 실패 시 None.
    """
    from .client import WolfClient

    cands = fetch_announced_domains(announcer_url, proxy_url=proxy_url,
                                    logger=logger)
    if not cands:
        return None
    for url in cands:
        try:
            cli = WolfClient(base_url=url, logger=logger,
                             proxy_url=proxy_url, cookies=cookies,
                             flaresolverr_url=flaresolverr_url)
            h = cli.check_health()
            if h['domain_ok'] and cli.looks_like_wolf_home():
                if logger:
                    logger.info('announcer 도메인 확정: %s', cli.base_url)
                return cli.base_url
        except Exception as e:
            if logger:
                logger.warning('announcer 후보 %s 검증 예외: %s', url, e)
            continue
    # health 는 통과 못 했지만 후보가 하나면 신뢰 (사이트 구조 변경 대비)
    if len(cands) == 1:
        if logger:
            logger.info('announcer 단일 후보 채택(검증 미통과): %s', cands[0])
        return cands[0]
    return None
