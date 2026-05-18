"""
오마이뉴스 TOP History 배치 분석 웹앱
실행: python app.py  →  http://localhost:5000
"""

import re
from datetime import datetime, timedelta, timezone
from html import unescape

KST = timezone(timedelta(hours=9))   # 한국 표준시
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

BASE_URL    = "https://www.ohmynews.com/NWS_Web/lastmainarticle/viewall.aspx?ID="
ARTICLE_URL = "https://www.ohmynews.com/NWS_Web/View/at_pg.aspx?CNTN_CD="

SECTION_LABELS = {
    'T0016': 'Top 기사',     # 최상단 5개 슬롯 (Top_1 ~ Top_5)
    'M0111': '오름(메인)',   # 좌측 대형 2개
    'M0112': '주요기사',
    'M0113': '오름(서브)',   # 우측 소형 4개
    'M0114': '중간', 'M0115': '중간2', 'M0116': '중간3',
    'M0117': '목록', 'M0118': '목록2', 'M0121': '기타',
    'M0122': '지역뉴스', 'M0128': '기타2', 'M0133': '기타3',
    'M0136': '기타4', 'M0137': '사는이야기', 'M0138': '책동네',
    'M0145': '스페셜콘텐츠', 'M0146': '스페셜콘텐츠2', 'M0147': '기타5',
    'M0151': 'SNS공유', 'M0152': '기타6', 'M0153': '기타7',
}

TOP_SECTIONS  = {'T0016'}              # Top 기사 (라이프사이클 추적 대상)
OEUM_SECTIONS = {'M0111', 'M0113'}     # 오름 (Top 이후 강등될 수 있는 다음 영역)
HOURS = [3, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]


# ── 데이터 수집 ────────────────────────────────────────────────────

def fetch_html(url):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except (HTTPError, URLError):
        return None


def extract_articles(html):
    """단일 시간대 HTML에서 (T0016 + M0xxx) 기사를 모두 추출"""
    seen = {}
    # T0016과 M0xxx 모두 캡처
    for m in re.finditer(r'CNTN_CD=(A\d+)[^"\']*CMPT_CD=([MT]\d+)', html):
        cntn, cmpt = m.group(1), m.group(2)
        if cntn not in seen:
            seen[cntn] = {'cntn_cd': cntn, 'cmpt_cd': cmpt, 'rank': len(seen) + 1}

    # Top_N 위치 추출 (T0016 기사용) — onclick 내부에 작은따옴표가 있어
    # [^>] 만 사용 (a 태그 닫는 > 전까지)
    top_pos = {}
    for m in re.finditer(
        r'CNTN_CD=(A\d+)[^>]*CMPT_CD=T0016[^>]*Top_(\d+)',
        html
    ):
        cntn, pos = m.group(1), int(m.group(2))
        if cntn not in top_pos:
            top_pos[cntn] = pos

    titles = {}
    # T0016: <img alt="..."> 속성에 제목 (HTML 엔티티 디코드 필요)
    for m in re.finditer(
        r'CNTN_CD=(A\d+)[^>]*CMPT_CD=T0016[^>]*>.{0,500}?<img[^>]*alt=[\'"]([^\'"]+)[\'"]',
        html, re.DOTALL
    ):
        cntn = m.group(1)
        title = unescape(m.group(2)).strip()
        if cntn not in titles and title and title != '이미지기사':
            titles[cntn] = title
    # M0111: class='p_tit'
    for m in re.finditer(
        r"CNTN_CD=(A\d+)[^'\"]*CMPT_CD=M0111[^'\"]*['\"]?\s*class=['\"]p_tit['\"][^>]*>([^<]{5,100})</a>",
        html
    ):
        cntn, title = m.group(1), m.group(2).strip()
        if cntn not in titles:
            titles[cntn] = title
    # M0113: <strong>
    for m in re.finditer(r'CNTN_CD=(A\d+)[^>]*CMPT_CD=M0113[^>]*>.*?<strong>(.*?)</strong>', html, re.DOTALL):
        cntn = m.group(1)
        title = re.sub(r'<[^>]+>', ' ', m.group(2)).strip()
        title = re.sub(r'\s+', ' ', title)
        if cntn not in titles and title:
            titles[cntn] = title
    # 그 외: <a> 텍스트
    for m in re.finditer(r'CNTN_CD=(A\d+)[^>]*>([^<]{5,80})</a>', html):
        cntn, title = m.group(1), m.group(2).strip()
        if cntn not in titles and not title.startswith('http'):
            titles[cntn] = title

    authors = {}
    for m in re.finditer(r'CNTN_CD=(A\d+).{0,500}?MEM_CD=\d+[^>]*>([^<]{2,20})</a>', html, re.DOTALL):
        cntn, author = m.group(1), m.group(2).strip()
        if cntn not in authors:
            authors[cntn] = author

    result = []
    for a in seen.values():
        cntn = a['cntn_cd']
        cmpt = a['cmpt_cd']
        result.append({
            'cntn_cd': cntn,
            'cmpt_cd': cmpt,
            'rank':    a['rank'],
            'section': SECTION_LABELS.get(cmpt, cmpt),
            'is_top':  cmpt in TOP_SECTIONS,
            'is_oeum': cmpt in OEUM_SECTIONS,
            'top_pos': top_pos.get(cntn),       # Top_1~Top_5 (T0016만)
            'title':   titles.get(cntn, ''),
            'author':  authors.get(cntn, ''),
            'url':     ARTICLE_URL + cntn,
        })
    return result


def fetch_slot(date_str, h):
    slot_id = f"{date_str}{h:02d}00"
    html = fetch_html(BASE_URL + slot_id)
    if html and len(re.findall(r'CNTN_CD=A\d+', html)) > 10:
        time_key = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {h:02d}:00"
        return time_key, extract_articles(html)
    return None, None


def collect_range(date_from_str, date_to_str):
    d_from = datetime.strptime(date_from_str, '%Y%m%d')
    d_to   = datetime.strptime(date_to_str,   '%Y%m%d')

    tasks = []
    d = d_from
    while d <= d_to:
        ds = d.strftime('%Y%m%d')
        for h in HOURS:
            tasks.append((ds, h))
        d += timedelta(days=1)

    slot_data = {}
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_slot, ds, h): (ds, h) for ds, h in tasks}
        for future in as_completed(futures):
            time_key, articles = future.result()
            if time_key and articles is not None:
                slot_data[time_key] = articles

    timeslots = sorted(slot_data.keys())
    articles = []
    for t in timeslots:
        for a in slot_data[t]:
            articles.append({'time': t, **a})

    return timeslots, articles


# ── Top 기사 라이프사이클 ───────────────────────────────────────────

def compute_lifecycle(articles, timeslots):
    """T0016(Top 기사)에 한 번이라도 등장한 기사들의 라이프사이클"""
    time_idx = {t: i for i, t in enumerate(timeslots)}

    # 분석 시각 (KST) — 1시간 미만 경과한 last_top은 '진행중'으로 판정
    analysis_now = datetime.now(KST).replace(tzinfo=None)

    meta    = {}
    by_time = defaultdict(dict)
    for a in articles:
        cntn = a['cntn_cd']
        by_time[cntn][a['time']] = a
        if cntn not in meta or (not meta[cntn]['title'] and a['title']):
            meta[cntn] = {'title': a['title'], 'author': a['author'], 'url': a['url']}

    # Top 기사에 한 번이라도 등장한 기사만
    top_cntns = {cntn for cntn, tm in by_time.items()
                 if any(r['is_top'] for r in tm.values())}

    result = []
    for cntn in top_cntns:
        tm = by_time[cntn]

        stages = []
        for t in timeslots:
            if t in tm:
                r = tm[t]
                stages.append({
                    'time':    t,
                    'state':   r['section'],
                    'cmpt_cd': r['cmpt_cd'],
                    'is_top':  r['is_top'],
                    'is_oeum': r['is_oeum'],
                    'top_pos': r['top_pos'],
                    'rank':    r['rank'],
                })

        top_stages = [s for s in stages if s['is_top']]
        if not top_stages:
            continue

        first_top = top_stages[0]['time']
        last_top  = top_stages[-1]['time']

        # 분석 시각 - last_top 이 1시간 미만이면 아직 판정 불가 ('진행중')
        last_top_dt = datetime.strptime(last_top, '%Y-%m-%d %H:%M')
        gap_hours = (analysis_now - last_top_dt).total_seconds() / 3600
        is_ongoing = gap_hours < 1

        # Top 이후 행방
        if is_ongoing:
            after_top = '진행중'
        else:
            last_idx  = time_idx.get(last_top, -1)
            after_top = '퇴장'
            if last_idx >= 0:
                for t in timeslots[last_idx + 1:]:
                    if t in tm:
                        after_top = tm[t]['section']
                        break

        # Top 진입 전 이력
        first_idx = time_idx.get(first_top, 0)
        pre_top_sections = list({tm[t]['section'] for t in timeslots[:first_idx] if t in tm})

        # Top 내 최고 위치 (Top_1이 가장 좋음)
        top_positions = [s['top_pos'] for s in top_stages if s['top_pos']]
        best_top_pos  = min(top_positions) if top_positions else None

        # 분류
        if after_top == '진행중':
            outcome = '진행중'
        elif after_top == '퇴장':
            outcome = '퇴장'
        elif after_top in ('오름(메인)', '오름(서브)'):
            outcome = '오름으로'
        elif after_top == '주요기사':
            outcome = '주요기사로'
        else:
            outcome = '기타섹션'

        result.append({
            'cntn_cd':       cntn,
            'title':         meta[cntn]['title'],
            'author':        meta[cntn]['author'],
            'url':           meta[cntn]['url'],
            'first_top':     first_top,
            'last_top':      last_top,
            'top_duration':  len(top_stages),
            'after_top':     after_top,
            'outcome':       outcome,
            'best_top_pos':  best_top_pos,
            'pre_top':       pre_top_sections,
            'stages':        stages,
        })

    result.sort(key=lambda x: time_idx.get(x['first_top'], 9999))
    return result


# ── Flask 라우트 ───────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.get_json()

    date_from = (data.get('date_from') or data.get('date') or '').replace('-', '')
    date_to   = (data.get('date_to')   or data.get('date') or '').replace('-', '')

    if not re.match(r'^\d{8}$', date_from) or not re.match(r'^\d{8}$', date_to):
        return jsonify({'error': '날짜 형식이 올바르지 않습니다 (YYYY-MM-DD)'}), 400

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    d_from = datetime.strptime(date_from, '%Y%m%d')
    d_to   = datetime.strptime(date_to,   '%Y%m%d')
    if (d_to - d_from).days > 13:
        return jsonify({'error': '최대 14일 범위까지 분석 가능합니다'}), 400

    timeslots, articles = collect_range(date_from, date_to)
    if not timeslots:
        return jsonify({'error': '해당 기간의 데이터가 없습니다'}), 404

    lifecycle = compute_lifecycle(articles, timeslots)

    multi_day = date_from != date_to
    slot_labels = {}
    for t in timeslots:
        parts = t.split(' ')
        slot_labels[t] = f"{parts[0][5:]} {parts[1]}" if multi_day else parts[1]

    return jsonify({
        'date_from':   date_from,
        'date_to':     date_to,
        'timeslots':   timeslots,
        'slot_labels': slot_labels,
        'articles':    articles,
        'lifecycle':   lifecycle,
    })


if __name__ == '__main__':
    print("http://localhost:5000 에서 실행 중")
    app.run(debug=False, port=5000)
