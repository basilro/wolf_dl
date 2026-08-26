from datetime import datetime

from sqlalchemy import UniqueConstraint, desc, or_

from .setup import *


class ModelWolfItem(ModelBase):
    """늑대닷컴 회차별 다운로드 이력.

    중복 방지 키: (work_kind, work_id, ep_url_id).
    늑대닷컴은 만화 단일이라 work_kind 는 항상 'manhwa'.
    work_id 는 toon 번호(숫자), ep_url_id 는 회차 num(숫자) — 문자열로 통일.
    (kind, work, ep) 가 전체 회차의 자연키.
    """
    P = P
    __tablename__ = 'wolf_dl_item'
    __table_args__ = (
        UniqueConstraint('work_kind', 'work_id', 'ep_url_id',
                         name='uq_wolf_dl_kind_work_ep'),
        {'mysql_collate': 'utf8_general_ci'},
    )
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    updated_time = db.Column(db.DateTime)

    # 작품
    work_kind = db.Column(db.String, index=True)   # 'manhwa' 고정
    work_id = db.Column(db.String, index=True)     # toon 번호 (예: '4461')
    work_title = db.Column(db.String)              # 작품명 (스냅샷)

    # 회차
    ep_url_id = db.Column(db.String, index=True)   # 회차 num (예: '211')
    episode_no = db.Column(db.Integer)             # 회차 번호 (정렬용)
    episode_title = db.Column(db.String)           # 회차 제목 (스냅샷)
    page_count = db.Column(db.Integer)             # 이미지 개수

    # 처리 상태: pending / downloading / completed / failed / partial / skipped_paid
    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    # 파일 저장
    save_dir = db.Column(db.String)            # 회차 폴더 (또는 zip 경로)
    downloaded_count = db.Column(db.Integer)
    total_bytes = db.Column(db.BigInteger)
    downloaded_at = db.Column(db.DateTime)

    def __init__(self):
        self.created_time = datetime.now()
        self.updated_time = self.created_time
        self.status = 'pending'
        self.downloaded_count = 0
        self.total_bytes = 0

    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        # 템플릿이 보내는 필드명은 option / search_word 인데 base.web_list 는
        # option1 / keyword 로 읽어서 search/option1 인자는 항상 'all'/'' 로 들어옴.
        # → req.form 에서 직접 읽어서 적용.
        query = db.session.query(cls)
        opt = (req.form.get('option') or option1 or 'all').strip()
        kw = (req.form.get('search_word') or req.form.get('keyword') or search or '').strip()
        if opt and opt != 'all':
            query = query.filter(cls.status == opt)
        if kw:
            pat = f'%{kw}%'
            query = query.filter(or_(cls.work_title.like(pat),
                                     cls.episode_title.like(pat)))
        if order == 'desc':
            query = query.order_by(desc(cls.id))
        else:
            query = query.order_by(cls.id)
        return query
