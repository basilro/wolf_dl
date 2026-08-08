"""웹훅 알림 발송 유틸 — Discord/Slack/일반 자동 분기."""
from typing import List, Dict

import requests


def send_webhook(url: str, message: str, username: str = 'wolf_dl',
                 timeout: int = 10) -> bool:
    """웹훅 URL 로 메시지 발송. URL 비어있으면 False 반환 (no-op).

    Discord / Slack / 기타 자동 분기:
      - discord.com/api/webhooks → {"content": msg, "username": ...}
      - hooks.slack.com         → {"text": msg}
      - 기타                     → {"content": msg, "text": msg}
    """
    if not url or not message:
        return False
    u = url.strip()
    try:
        if 'discord.com/api/webhooks' in u or 'discordapp.com/api/webhooks' in u:
            payload = {'content': message, 'username': username}
        elif 'hooks.slack.com' in u:
            payload = {'text': message}
        else:
            payload = {'content': message, 'text': message}
        r = requests.post(u, json=payload, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def build_download_summary(completed_items: List[Dict]) -> str:
    """완료된 다운로드 항목 list → 발송용 텍스트.

    completed_items: [{'series_title': str, 'episode_title': str,
                       'episode_no': int}, ...]
    """
    if not completed_items:
        return ''
    grouped: Dict[str, List[Dict]] = {}
    for it in completed_items:
        s = it.get('series_title') or '(unknown)'
        grouped.setdefault(s, []).append(it)

    total = len(completed_items)
    header = f'[늑대닷컴] 다운로드 완료 — 총 {total}회차'
    lines: List[str] = [header]
    for series_title, eps in sorted(grouped.items()):
        eps_sorted = sorted(eps, key=lambda x: x.get('episode_no') or 0)
        cnt = len(eps_sorted)
        if cnt <= 5:
            titles = ', '.join((e.get('episode_title') or '?')
                               for e in eps_sorted)
        else:
            first = eps_sorted[0].get('episode_title') or '?'
            last = eps_sorted[-1].get('episode_title') or '?'
            titles = f'{first} ~ {last}'
        lines.append(f'- {series_title} ({cnt}): {titles}')
    return '\n'.join(lines)
