"""늑대닷컴 HTTP 클라이언트 — 만화 단일.

- 무로그인
- 핑거프린트 쿠키 자동 세팅
- 도메인은 로테이션되므로 `base_url` 외부에서 주입.

URL 구조:
  - 작품(회차목록): {base}/list?toon={work_id}&s=o&pg={n}
  - 회차 뷰어:      {base}/view?toon={work_id}&num={num}
  - 본문 이미지:    별도 CDN(acloud10/bacloud2 등) — Referer 는 {base}/
"""
import html as html_lib
import json
import re
import secrets
import time
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse

import requests

# ── HTTP 백엔드 ──
# Cloudflare TLS 핑거프린트 우회를 위해 curl_cffi (Chrome 임퍼소네이트) 우선 사용.
# 미설치 환경에서는 stdlib requests 로 폴백.
try:
    from curl_cffi import requests as _cffi_req
    _HTTP_BACKEND = 'curl_cffi'
except Exception:
    _cffi_req = None
    _HTTP_BACKEND = 'requests'


DEFAULT_BASE = 'https://wfwf436.com'

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36')

# 만화 단일 사이트지만 toki_dl 뼈대(kind 개념)와의 호환을 위해 유지.
KIND_LABEL = {'manhwa': '만화'}
DEFAULT_KIND = 'manhwa'

# 회차 목록 페이지네이션 안전 상한
_MAX_LIST_PAGES = 30


class WolfError(Exception):
    pass


class NotReadableError(WolfError):
    """회차 잠금/공개예정 — 본문 이미지 없음."""


class BlockedError(WolfError):
    """서버 차단 (핑거프린트 누락, IP 블랙 등) — 재시도 권장."""


# ───────────────────────── FlareSolverr proxy ─────────────────────────

class _FSResponse:
    """FlareSolverr 응답을 requests.Response 와 비슷한 인터페이스로 wrap."""

    def __init__(self, sol: dict):
        self.status_code = int(sol.get('status') or 0)
        text = sol.get('response') or ''
        self.text = text
        try:
            self.content = text.encode('utf-8')
        except Exception:
            self.content = b''
        self.url = sol.get('url') or ''
        self._headers_list = sol.get('headers') or []
        self._cookies_list = sol.get('cookies') or []

    @property
    def headers(self):
        return _FSHeaderView(self._headers_list)

    def json(self):
        text = self.text
        m = re.search(r'<pre[^>]*>(.*?)</pre>', text, re.DOTALL)
        if m:
            text = m.group(1)
            text = (text.replace('&quot;', '"').replace('&amp;', '&')
                        .replace('&lt;', '<').replace('&gt;', '>'))
        return json.loads(text)


class _FSHeaderView:
    """대소문자 무시 header 조회 — 중복 키는 ', ' 로 합침."""

    def __init__(self, items):
        self._items = items

    def get(self, name, default=None):
        ln = name.lower()
        matches = [it.get('value', '') for it in self._items
                   if (it.get('name') or '').lower() == ln]
        if not matches:
            return default
        return ', '.join(matches)


class _FSCookieJar:
    """최소 호환 cookie jar — FS 모드에선 실제 cookie 는 FS 가 관리."""

    def __init__(self):
        self._d = {}

    def set(self, name, value, **kw):
        self._d[name] = value

    def get(self, name, default=None):
        return self._d.get(name, default)


class FlareSolverrSession:
    """requests.Session 호환 — 모든 요청을 FlareSolverr 로 proxy.

    이미지 같은 binary 응답은 처리하지 못함 (호출자가 직접 다른 세션 사용).
    HTML/JSON API 응답만 안정적.
    """

    def __init__(self, fs_url: str, base_url: str,
                 init_cookies: Optional[List[Dict[str, str]]] = None,
                 proxy_url: Optional[str] = None,
                 logger=None):
        self.fs_url = fs_url.rstrip('/') + '/v1'
        self.base_url = base_url
        self.logger = logger
        self.headers = {}                # mock — FS 는 헤더 일부만 인식
        self.cookies = _FSCookieJar()    # mock
        self.proxies = {}                # mock
        self._session_id: Optional[str] = None
        self._pending_cookies = list(init_cookies or [])
        body: Dict[str, Any] = {'cmd': 'sessions.create'}
        if proxy_url:
            body['proxy'] = {'url': proxy_url}
        try:
            r = requests.post(self.fs_url, json=body, timeout=120)
        except Exception as e:
            raise WolfError(f'FlareSolverr 접속 실패: {e}')
        try:
            data = r.json()
        except Exception:
            raise WolfError(
                f'FlareSolverr non-JSON: HTTP {r.status_code} {r.text[:120]}')
        if data.get('status') != 'ok':
            raise WolfError(
                f'FlareSolverr sessions.create: {data.get("message")}')
        self._session_id = data.get('session')
        if logger:
            logger.info('[FS] session created: %s', self._session_id)

    def close(self):
        if not self._session_id:
            return
        try:
            requests.post(self.fs_url, json={
                'cmd': 'sessions.destroy', 'session': self._session_id,
            }, timeout=15)
        except Exception:
            pass
        self._session_id = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def get(self, url, headers=None, timeout=30, **kw):
        return self._req('request.get', url, headers, timeout)

    def post(self, url, data=None, headers=None, timeout=30, **kw):
        return self._req('request.post', url, headers, timeout, data)

    def _req(self, cmd: str, url: str,
             headers: Optional[Dict[str, str]],
             timeout: Optional[int],
             post_data=None) -> _FSResponse:
        t = int(timeout or 30)
        body: Dict[str, Any] = {
            'cmd': cmd, 'url': url, 'session': self._session_id,
            'maxTimeout': max(t * 1000, 60000),
        }
        if self._pending_cookies:
            body['cookies'] = self._pending_cookies
            self._pending_cookies = []
        if post_data is not None:
            if isinstance(post_data, (dict, list)):
                from urllib.parse import urlencode
                post_data = urlencode(post_data)
            elif not isinstance(post_data, str):
                post_data = str(post_data)
            body['postData'] = post_data
        if headers:
            body['headers'] = headers
        try:
            r = requests.post(self.fs_url, json=body, timeout=t + 90)
        except Exception as e:
            raise WolfError(f'FlareSolverr 요청 실패: {e}')
        try:
            data = r.json()
        except Exception:
            raise WolfError(
                f'FlareSolverr non-JSON 응답: HTTP {r.status_code}')
        if data.get('status') != 'ok':
            msg = data.get('message') or ''
            if 'challenge' in msg.lower() or 'cloudflare' in msg.lower():
                raise BlockedError(f'FlareSolverr challenge 실패: {msg}')
            raise WolfError(f'FlareSolverr: {msg}')
        return _FSResponse(data.get('solution') or {})


# ───────────────────────────── client ──────────────────────────────


class WolfClient:

    def __init__(self, base_url: Optional[str] = None,
                 logger=None, proxy_url: Optional[str] = None,
                 cookies: Optional[str] = None,
                 flaresolverr_url: Optional[str] = None):
        self.base_url = (base_url or DEFAULT_BASE).rstrip('/')
        self.logger = logger
        self._proxy_url = (proxy_url or '').strip() or None
        self._user_cookies = (cookies or '').strip() or None
        self._fs_url = (flaresolverr_url or '').strip() or None
        # 세션 단위로 고정해두는 핑거프린트 (브라우저 client-side 쿠키 모방)
        self._wf_pid = secrets.token_hex(16)
        self._wf_fp = secrets.token_hex(16)
        # 이미지 CDN — Cloudflare 보호 없음, 항상 직접 호출
        self._img_sess = self._build_direct_session()
        # 메인 도메인 — FS 설정 시 proxy, 아니면 직접
        if self._fs_url:
            self._sess = self._build_fs_session()
        else:
            self._sess = self._img_sess

    # ---- 로깅 ----
    def _log(self, level: str, msg: str, *args):
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg, *args)
        else:
            print(f'[{level.upper()}] ' + (msg % args if args else msg))

    # ---- URL 헬퍼 ----
    def list_url(self, work_id: str, sort: str = 'o', pg: int = 1) -> str:
        return f'{self.base_url}/list?toon={work_id}&s={sort}&pg={pg}'

    def view_url(self, work_id: str, num: str) -> str:
        return f'{self.base_url}/view?toon={work_id}&num={num}'

    def home_referer(self) -> str:
        return f'{self.base_url}/'

    # ---- 세션 / 헤더 ----
    def _build_direct_session(self):
        """직접 호출 세션 — curl_cffi (Chrome TLS) 우선, fallback requests."""
        if _HTTP_BACKEND == 'curl_cffi':
            s = _cffi_req.Session(impersonate='chrome')
            s.headers.update({
                'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
            })
        else:
            s = requests.Session()
            s.headers.update({
                'User-Agent': DEFAULT_UA,
                'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate',
            })
        host = urlparse(self.base_url).netloc
        for name, val in (('wf_pid', self._wf_pid), ('wf_fp', self._wf_fp)):
            try:
                s.cookies.set(name, val, domain=host, path='/')
            except Exception:
                pass
        if self._user_cookies:
            for name, val in self._parse_cookie_string(self._user_cookies):
                try:
                    s.cookies.set(name, val, domain=host, path='/')
                except Exception:
                    pass
        if self._proxy_url:
            s.proxies = {'http': self._proxy_url, 'https': self._proxy_url}
        return s

    def _build_fs_session(self):
        """FlareSolverr proxy 세션 — Cloudflare 챌린지 우회용."""
        init_cookies: List[Dict[str, str]] = [
            {'name': 'wf_pid', 'value': self._wf_pid},
            {'name': 'wf_fp',  'value': self._wf_fp},
        ]
        if self._user_cookies:
            for n, v in self._parse_cookie_string(self._user_cookies):
                init_cookies.append({'name': n, 'value': v})
        return FlareSolverrSession(
            fs_url=self._fs_url, base_url=self.base_url,
            init_cookies=init_cookies, proxy_url=self._proxy_url,
            logger=self.logger)

    @staticmethod
    def _decode_html(r) -> str:
        """응답 HTML 을 올바른 문자셋으로 디코드.

        늑대닷컴은 본문이 EUC-KR(cp949)인데 응답 헤더가 `Charset=euc-kr`
        (대문자 C)로 와서 curl_cffi 가 charset 을 못 잡고 utf-8 로 디코드 →
        한글이 깨진다. 그래서 bytes(`content`)를 직접 받아 문자셋을
        헤더/meta/기본값(euc-kr) 순으로 판별해 디코드한다.
        FlareSolverr 응답은 이미 유니코드로 렌더돼 오므로 `.text` 사용.
        """
        if isinstance(r, _FSResponse):
            return r.text or ''
        content = getattr(r, 'content', None)
        if not content:
            return getattr(r, 'text', '') or ''
        enc = None
        # 1) Content-Type charset (대소문자 무시)
        try:
            ct = r.headers.get('content-type') or r.headers.get('Content-Type') or ''
        except Exception:
            ct = ''
        m = re.search(r'charset\s*=\s*["\']?\s*([\w\-]+)', ct or '', re.I)
        if m:
            enc = m.group(1)
        # 2) 본문 <meta charset=...>
        if not enc:
            m = re.search(rb'charset\s*=\s*["\']?\s*([\w\-]+)', content[:2048], re.I)
            if m:
                try:
                    enc = m.group(1).decode('ascii', 'ignore')
                except Exception:
                    enc = None
        # 3) 기본값: 늑대닷컴은 euc-kr
        if not enc:
            enc = 'euc-kr'
        el = enc.strip().lower()
        if el in ('euc-kr', 'euckr', 'ks_c_5601-1987', 'ksc5601'):
            el = 'cp949'   # cp949 가 euc-kr 상위호환 — 더 안전
        try:
            return content.decode(el, errors='replace')
        except Exception:
            return content.decode('cp949', errors='replace')

    @staticmethod
    def _parse_cookie_string(raw: str) -> List[Tuple[str, str]]:
        """`k1=v1; k2=v2` / 줄바꿈 / 콤마 구분 → [(name, value), ...]"""
        out: List[Tuple[str, str]] = []
        if not raw:
            return out
        chunks = re.split(r'[;\n]+', raw.strip())
        for c in chunks:
            c = c.strip()
            if not c or '=' not in c:
                continue
            name, _, val = c.partition('=')
            name = name.strip()
            val = val.strip().strip('"')
            if name:
                out.append((name, val))
        return out

    def _html_headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        h = {
            'Accept': ('text/html,application/xhtml+xml,application/xml;'
                       'q=0.9,image/avif,image/webp,*/*;q=0.8'),
            'upgrade-insecure-requests': '1',
        }
        if _HTTP_BACKEND != 'curl_cffi':
            h.update({
                'sec-ch-ua': ('"Chromium";v="150", "Google Chrome";v="150", '
                              '"Not/A)Brand";v="99"'),
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'same-origin' if referer else 'none',
                'sec-fetch-user': '?1',
            })
        if referer:
            h['Referer'] = referer
        return h

    # ───────────────────── URL / ID 추출 ─────────────────────

    @staticmethod
    def extract_work_id(token: str,
                        default_kind: str = DEFAULT_KIND
                        ) -> Optional[Tuple[str, str]]:
        """입력 토큰 → (kind, work_id). work_id 는 toon 번호(문자열).

        지원:
          - 'https://wfwf436.com/list?toon=4461' → ('manhwa', '4461')
          - 'https://wfwf436.com/view?toon=4461&num=211' → ('manhwa', '4461')
          - '?toon=4461&num=1' → ('manhwa', '4461')
          - '4461' → ('manhwa', '4461')
        """
        s = (token or '').strip()
        if not s:
            return None
        m = re.search(r'[?&]toon=(\d+)', s)
        if m:
            return (DEFAULT_KIND, m.group(1))
        if re.fullmatch(r'\d+', s):
            return (DEFAULT_KIND, s)
        return None

    @staticmethod
    def extract_episode_url_id(token: str
                               ) -> Optional[Tuple[str, str, str]]:
        """뷰어 URL → (kind, work_id, num). 수동 다운에서 쓰임."""
        s = (token or '').strip()
        mt = re.search(r'[?&]toon=(\d+)', s)
        mn = re.search(r'[?&]num=(\d+)', s)
        if mt and mn:
            return (DEFAULT_KIND, mt.group(1), mn.group(1))
        return None

    # ───────────────────── 작품 / 회차 목록 ─────────────────────

    def get_work(self, kind: str, work_id: str) -> Dict[str, Any]:
        """작품 페이지 → 메타 + 회차 목록 통합 반환.

        페이지네이션(`&s=o&pg=N`)을 순회해 전체 회차 수집.

        반환:
          {
            'kind': 'manhwa',
            'work_id': str,
            'title': str,
            'thumb': str,            # cover 이미지 URL (있을 경우)
            'description': str,
            'author': str,
            'genres': [str, ...],
            'completed': bool|None,
            'episodes': [
              {'ep_url_id': str, 'no': int, 'title': str, 'paid': bool}, ...
            ],  # 정렬: 회차 번호 오름차순
          }
        """
        first_url = self.list_url(work_id, sort='o', pg=1)
        r = self._sess.get(first_url, timeout=20,
                           headers=self._html_headers(
                               referer=self.home_referer()))
        if r.status_code == 404:
            raise WolfError(f'work not found: {work_id}')
        if r.status_code != 200:
            raise WolfError(f'work HTTP {r.status_code}: {work_id}')
        html = self._decode_html(r)
        body_lower = html[:5000].lower()
        if 'just a moment' in body_lower or 'cdn-cgi/challenge' in body_lower:
            raise BlockedError(f'cf challenge on work page: {work_id}')

        meta = self._parse_work_meta(html, work_id)

        # 1페이지 회차 + 페이지네이션 순회
        eps_map: Dict[str, Dict[str, Any]] = {}
        for ep in self._parse_episodes(html):
            eps_map[ep['ep_url_id']] = ep
        last_page = self._detect_last_page(html)

        pg = 2
        while pg <= min(last_page, _MAX_LIST_PAGES):
            url = self.list_url(work_id, sort='o', pg=pg)
            try:
                rp = self._sess.get(url, timeout=20,
                                    headers=self._html_headers(
                                        referer=first_url))
            except Exception as e:
                self._log('warning', 'list pg=%d 요청 실패: %s', pg, e)
                break
            if rp.status_code != 200:
                break
            page_eps = self._parse_episodes(self._decode_html(rp))
            new_cnt = 0
            for ep in page_eps:
                if ep['ep_url_id'] not in eps_map:
                    eps_map[ep['ep_url_id']] = ep
                    new_cnt += 1
            if not page_eps or new_cnt == 0:
                break
            pg += 1

        eps = list(eps_map.values())
        eps.sort(key=lambda e: e['no'])
        meta['episodes'] = eps
        return meta

    @staticmethod
    def _detect_last_page(html: str) -> int:
        """페이지네이션(.pg-btn) 에서 최대 페이지 번호. 없으면 1."""
        last = 1
        for m in re.finditer(r'[?&]pg=(\d+)', html):
            n = int(m.group(1))
            if n > last:
                last = n
        return last

    def _parse_work_meta(self, html: str, work_id: str) -> Dict[str, Any]:
        """작품 페이지 HTML 에서 작품 정보 추출."""
        title = ''
        m = re.search(r'<h1 class="w-title">([^<]+)</h1>', html)
        if m:
            title = html_lib.unescape(m.group(1)).strip()
        if not title:
            m = re.search(r'<title>([^<|]+?)(?:\s*-\s*늑대닷컴)?\s*</title>',
                          html)
            if m:
                title = m.group(1).strip()

        # 줄거리
        desc = ''
        m = re.search(
            r'<div class="summary"[^>]*>(.*?)</div>', html, re.DOTALL)
        if m:
            desc = html_lib.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()

        # 썸네일 — <div class="thumb-wrap"><img src="...">
        thumb = self._extract_cover_url(html)

        # 장르 — <a class="gtag" ...>#드라마</a>
        genres: List[str] = []
        for gm in re.finditer(r'<a class="gtag"[^>]*>#?([^<]+)</a>', html):
            g = html_lib.unescape(gm.group(1)).strip().lstrip('#').strip()
            if g:
                genres.append(g)

        return {
            'kind': DEFAULT_KIND, 'work_id': work_id,
            'title': title or f'만화_{work_id}',
            'thumb': thumb, 'description': desc, 'author': '',
            'genres': genres, 'completed': None,
        }

    @staticmethod
    def _extract_cover_url(html: str) -> str:
        """작품 페이지 HTML 에서 cover URL 추출. 못 찾으면 ''."""
        m = re.search(
            r'<div[^>]*class="[^"]*thumb-wrap[^"]*"[^>]*>\s*<img[^>]+src="([^"]+)"',
            html, re.DOTALL)
        if m:
            return html_lib.unescape(m.group(1)).strip()
        m = re.search(
            r'<meta property="og:image"[^>]*content="([^"]+)"', html)
        if m:
            return html_lib.unescape(m.group(1)).strip()
        return ''

    @staticmethod
    def _parse_episodes(html: str) -> List[Dict[str, Any]]:
        """회차 목록 (`<a class="ep-item">` 앵커).

        <a class="ep-item" href="/view?toon=4461&num=220" data-num="220">
          <div class="ep-num">220</div>
          <div class="ep-content"><span class="ep-title">비뢰도 220화</span></div>
          <div class="ep-date">2025-12-25</div>
        </a>
        """
        out: List[Dict[str, Any]] = []
        pattern = re.compile(
            r'<a class="ep-item"[^>]*\bhref="[^"]*[?&]num=(\d+)"[^>]*>'
            r'.*?<span class="ep-title">([^<]*)</span>',
            re.DOTALL)
        seen = set()
        for m in pattern.finditer(html):
            num = m.group(1)
            if num in seen:
                continue
            seen.add(num)
            title = html_lib.unescape(m.group(2)).strip()
            out.append({'ep_url_id': num, 'no': int(num),
                        'title': title or f'{num}화', 'paid': False})
        return out

    # ───────────────────── 작품 검색 ─────────────────────

    def search(self, query: str, page: int = 1) -> List[Dict[str, Any]]:
        """제목 검색 → 작품 카드 리스트.

        검색어는 EUC-KR(cp949) 로 URL 인코딩해야 함 (`/sh?q=...`).

        반환: [{'work_id': str, 'title': str, 'thumb': str,
                'genres': str, 'last_episode': str, 'completed': bool}, ...]
        """
        from urllib.parse import quote
        q = (query or '').strip()
        if not q:
            return []
        try:
            enc = quote(q, encoding='euc-kr')
        except Exception:
            enc = quote(q)
        url = (f'{self.base_url}/sh?t2=&t3=&o=n&pg={int(page)}&q={enc}')
        r = self._sess.get(url, timeout=20,
                           headers=self._html_headers(
                               referer=self.home_referer()))
        if r.status_code != 200:
            raise WolfError(f'search HTTP {r.status_code}: {q}')
        html = self._decode_html(r)
        body_lower = html[:5000].lower()
        if 'just a moment' in body_lower or 'cdn-cgi/challenge' in body_lower:
            raise BlockedError(f'cf challenge on search: {q}')
        return self._parse_search_cards(html)

    # 목록 필터 상수 (UI/검증 공용)
    LIST_STATUS = {'ing': '연재', 'end': '완결'}
    WEEKDAY = {'': '전체', '1': '월', '2': '화', '3': '수', '4': '목',
               '5': '금', '6': '토', '7': '일', '10': '기타'}
    RATING = {'': '전체', '1': '일반', '2': 'BL', '3': '성인'}
    SORT = {'n': '최신순', 'r': '신작순', 'f': '인기순'}
    GENRES = ['드라마', '판타지', '액션', '로맨스', '일상', '개그', '미스터리',
              '순정', '스포츠', '스릴러', '무협', '학원', '공포', '스토리']

    def browse(self, status: str = 'ing', t1: str = '', t2: str = '',
               t3: str = '', o: str = 'n', pg: int = 1) -> Dict[str, Any]:
        """연재(`/ing`)·완결(`/end`) 목록 + 필터 조회.

        - status: 'ing'(연재) | 'end'(완결)
        - t1: 요일 ('' 전체, '1'~'7' 월~일, '10' 기타) — 완결엔 보통 무의미
        - t2: 구분 ('' 전체, '1' 일반, '2' BL, '3' 성인)
        - t3: 장르명 (EUC-KR 로 인코딩됨. '' 전체)
        - o : 정렬 ('n' 최신, 'r' 신작, 'f' 인기)
        - pg: 페이지

        반환: {'cards': [...], 'last_page': int, 'page': int, 'status': str}
        """
        from urllib.parse import quote
        path = 'end' if str(status) == 'end' else 'ing'
        t3 = (t3 or '').strip()
        try:
            t3enc = quote(t3, encoding='euc-kr') if t3 else ''
        except Exception:
            t3enc = quote(t3) if t3 else ''
        o = o if o in self.SORT else 'n'
        url = (f'{self.base_url}/{path}?t1={t1}&t2={t2}&t3={t3enc}'
               f'&o={o}&pg={int(pg)}')
        r = self._sess.get(url, timeout=20,
                           headers=self._html_headers(
                               referer=self.home_referer()))
        if r.status_code != 200:
            raise WolfError(f'browse HTTP {r.status_code}: /{path}')
        html = self._decode_html(r)
        body_lower = html[:5000].lower()
        if 'just a moment' in body_lower or 'cdn-cgi/challenge' in body_lower:
            raise BlockedError(f'cf challenge on browse: /{path}')
        return {
            'cards': self._parse_search_cards(html),
            'last_page': self._detect_last_page(html),
            'page': int(pg), 'status': path,
        }

    @staticmethod
    def _parse_search_cards(html: str) -> List[Dict[str, Any]]:
        """검색/목록 그리드의 `<a class="t-card">` 카드 파싱."""
        out: List[Dict[str, Any]] = []
        # 각 카드 블록 단위로 분리
        for cm in re.finditer(
                r'<a class="t-card"[^>]*\bhref="[^"]*[?&]toon=(\d+)"[^>]*>'
                r'(.*?)</a>', html, re.DOTALL):
            work_id = cm.group(1)
            block = cm.group(2)
            title = ''
            m = re.search(r'<div class="t-title">([^<]*)</div>', block)
            if m:
                title = html_lib.unescape(m.group(1)).strip()
            if not title:
                m = re.search(r'<img[^>]+alt="([^"]*)"', block)
                if m:
                    title = html_lib.unescape(m.group(1)).strip()
            thumb = ''
            m = re.search(r'<img[^>]+src="([^"]+)"', block)
            if m:
                thumb = html_lib.unescape(m.group(1)).strip()
            genres = ''
            m = re.search(r'<div class="t-genre">([^<]*)</div>', block)
            if m:
                genres = html_lib.unescape(m.group(1)).strip()
            last_ep = ''
            m = re.search(r'<div class="t-ep">([^<]*)</div>', block)
            if m:
                last_ep = html_lib.unescape(m.group(1)).strip()
            completed = 'badge-end' in block
            adult = 'badge-19' in block
            out.append({
                'work_id': work_id, 'title': title or f'만화_{work_id}',
                'thumb': thumb, 'genres': genres,
                'last_episode': last_ep, 'completed': completed,
                'adult': adult,
            })
        return out

    # ───────────────────── 회차 뷰어 ─────────────────────

    def get_episode_images(self, kind: str, work_id: str,
                           ep_url_id: str) -> Tuple[List[str], str]:
        """회차 뷰어 → (이미지 URL 리스트, 회차 제목).

        본문 이미지가 없으면 NotReadableError (잠금/공개예정 등).
        """
        url = self.view_url(work_id, ep_url_id)
        r = self._sess.get(url, timeout=20,
                           headers=self._html_headers(
                               referer=self.list_url(work_id)))
        if r.status_code == 404:
            raise NotReadableError(f'episode 404: {work_id}/{ep_url_id}')
        if r.status_code != 200:
            raise WolfError(f'viewer HTTP {r.status_code}: '
                            f'{work_id}/{ep_url_id}')
        html = self._decode_html(r)
        body_lower = html[:5000].lower()
        if 'just a moment' in body_lower or 'cdn-cgi/challenge' in body_lower:
            raise BlockedError(f'cf challenge on viewer: {work_id}/{ep_url_id}')

        urls = self._parse_images(html)
        if not urls:
            raise NotReadableError(f'no images in viewer: '
                                   f'{work_id}/{ep_url_id}')
        # 회차 제목 — <title>211화 - 늑대닷컴</title>
        subtitle = ''
        m = re.search(r'<title>([^<]+?)\s*-\s*늑대닷컴\s*</title>', html)
        if m:
            subtitle = m.group(1).strip()
        else:
            m = re.search(r'<div class="vbar-title">([^<]+)</div>', html)
            if m:
                subtitle = html_lib.unescape(m.group(1)).strip()
        return urls, subtitle

    @staticmethod
    def _parse_images(html: str) -> List[str]:
        """뷰어 본문 `.vimg-area` 안 `<img data-src="<url>">` 순서대로.

        `data-src` 가 실제 이미지, `src` 는 placeholder(sprite.png).
        """
        # 본문 영역만 추출 (광고 img 제외)
        m = re.search(
            r'<div[^>]*class="[^"]*vimg-area[^"]*"[^>]*>(.*?)</div>\s*(?:<|$)',
            html, re.DOTALL)
        area = m.group(1) if m else html
        urls: List[str] = []
        for im in re.finditer(r'<img[^>]+data-src="([^"]+)"', area):
            u = html_lib.unescape(im.group(1)).strip()
            if u and not u.lower().endswith('sprite.png'):
                urls.append(u)
        # 중복 제거 (순서 보존)
        return list(dict.fromkeys(urls))

    # ───────────────────── 이미지 다운로드 ─────────────────────

    def download_image(self, url: str, referer: str,
                       max_retries: int = 2) -> bytes:
        """이미지 1장 다운로드 (요청별 Referer 필수)."""
        img_headers = {
            'Accept': ('image/avif,image/webp,image/apng,'
                       'image/svg+xml,image/*,*/*;q=0.8'),
            'Referer': referer,
        }
        if _HTTP_BACKEND != 'curl_cffi':
            img_headers.update({
                'sec-fetch-dest': 'image',
                'sec-fetch-mode': 'no-cors',
                'sec-fetch-site': 'cross-site',
            })
        sess = self._img_sess
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = sess.get(url, timeout=30, headers=img_headers)
                if r.status_code == 200:
                    return r.content
                last_err = WolfError(
                    f'image HTTP {r.status_code}: {url[:120]}')
            except Exception as e:
                last_err = WolfError(f'image fetch fail: {e}')
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
        raise last_err or WolfError(f'image fetch fail: {url[:120]}')

    @staticmethod
    def url_ext(url: str) -> str:
        """이미지 URL → 확장자 (`.jpg`/`.png`/...)."""
        m = re.search(r'\.([a-zA-Z0-9]{2,5})(?:\?|$)', url or '')
        if not m:
            return '.jpg'
        ext = '.' + m.group(1).lower()
        if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
            return ext
        return '.jpg'

    # ───────────────────── 디버그 / 진단 ─────────────────────

    def ping(self) -> bool:
        """base_url 이 살아있는지 가벼운 GET 으로 확인."""
        try:
            r = self._sess.get(self.base_url + '/', timeout=10,
                               headers=self._html_headers())
            return r.status_code == 200
        except Exception as e:
            self._log('info', 'ping 실패: %s', e)
            return False

    def check_health(self) -> Dict[str, Any]:
        """도메인 + 쿠키 헬스 체크.

        반환:
          {'domain_ok': bool, 'cookies_ok': bool|None,
           'reason': str, 'status_code': int|None}
        """
        result: Dict[str, Any] = {
            'domain_ok': False, 'cookies_ok': None,
            'reason': '', 'status_code': None,
        }
        try:
            r = self._sess.get(self.base_url + '/', timeout=10,
                               headers=self._html_headers())
        except Exception as e:
            result['reason'] = f'접속 불가: {e}'
            return result
        result['status_code'] = r.status_code
        if r.status_code != 200:
            result['reason'] = f'HTTP {r.status_code} — 도메인 변경/만료 의심'
            return result
        body_head = self._decode_html(r)[:8000].lower()
        if ('just a moment' in body_head
                or 'cdn-cgi/challenge' in body_head
                or ('cloudflare' in body_head and 'challenge' in body_head)):
            result['domain_ok'] = True
            result['cookies_ok'] = False
            result['reason'] = 'Cloudflare 챌린지 — 쿠키/핑거프린트 만료 의심'
            return result
        result['domain_ok'] = True
        result['cookies_ok'] = True
        return result

    def looks_like_wolf_home(self) -> bool:
        """homepage 본문에서 늑대닷컴 시그니처를 가볍게 검사."""
        try:
            r = self._sess.get(self.base_url + '/', timeout=15,
                               headers=self._html_headers())
        except Exception:
            return False
        if r.status_code != 200:
            return False
        body = self._decode_html(r)[:80000]
        low = body.lower()
        if 'just a moment' in low or 'cdn-cgi/challenge' in low:
            return False
        hits = 0
        for sig in ('늑대닷컴', 'toon=', '/list?', '/view?', 'ep-item'):
            if sig in body:
                hits += 1
        return hits >= 2

    @staticmethod
    def increment_base_url_candidates(current_base_url: str,
                                      max_try: int = 5) -> List[str]:
        """현재 base_url 의 호스트 끝 숫자를 +1 ~ +max_try 증가시킨 후보 반환.

        예) https://wfwf436.com → [wfwf437.com, wfwf438.com, ...]
        """
        if not current_base_url:
            return []
        parsed = urlparse(current_base_url)
        host = parsed.netloc
        scheme = parsed.scheme or 'https'
        m = re.search(r'(\d+)((?:\.[a-z]+)+)$', host, re.IGNORECASE)
        if not m:
            return []
        num_str = m.group(1)
        suffix = m.group(2)
        prefix = host[:m.start(1)]
        cur = int(num_str)
        width = len(num_str)
        out: List[str] = []
        for delta in range(1, max_try + 1):
            n = cur + delta
            new_num = str(n).zfill(width) if len(str(n)) <= width else str(n)
            out.append(f'{scheme}://{prefix}{new_num}{suffix}')
        return out

    @classmethod
    def resolve_base_url(cls, current_base_url: str,
                         proxy_url: Optional[str] = None,
                         cookies: Optional[str] = None,
                         flaresolverr_url: Optional[str] = None,
                         max_try: int = 5,
                         logger=None) -> Optional[str]:
        """호스트 끝 숫자를 증가시키며 최초로 살아있는 도메인 반환.

        각 후보에 대해 `check_health()` + `looks_like_wolf_home()` 검증.
        모두 실패 시 None.
        """
        cands = cls.increment_base_url_candidates(current_base_url,
                                                  max_try=max_try)
        if not cands:
            if logger:
                logger.warning('도메인 자동 갱신 불가 — 호스트 끝 숫자 없음: %s',
                               current_base_url)
            return None
        if logger:
            logger.info('도메인 증가 후보 %d개: %s ...', len(cands),
                        ', '.join(cands[:3]))
        for url in cands:
            try:
                cli = cls(base_url=url, logger=logger,
                          proxy_url=proxy_url, cookies=cookies,
                          flaresolverr_url=flaresolverr_url)
                h = cli.check_health()
                if not h['domain_ok']:
                    if logger:
                        logger.info('도메인 %s — %s', url, h['reason'])
                    continue
                if not cli.looks_like_wolf_home():
                    if logger:
                        logger.info('도메인 %s — 시그니처 미일치', url)
                    continue
                if logger:
                    logger.info('도메인 자동 갱신 성공: %s', url)
                return cli.base_url
            except Exception as e:
                if logger:
                    logger.warning('도메인 %s 검증 예외: %s', url, e)
                continue
        return None

    # ---- 설정값 정규화 ----
    @staticmethod
    def resolve_proxy(use_proxy, proxy_url) -> str:
        """설정값 → 실제 프록시 URL. use_proxy=True 이고 URL 있을 때만."""
        try:
            enabled = (str(use_proxy or 'False').strip() == 'True')
        except Exception:
            enabled = False
        if not enabled:
            return ''
        return (proxy_url or '').strip()
