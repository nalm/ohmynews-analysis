"""
오마이뉴스 TOP History 배치 분석 웹앱
실행: python app.py  →  http://localhost:5000
"""

import re
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

BASE_URL    = "https://www.ohmynews.com/NWS_Web/lastmainarticle/viewall.aspx?ID="
ARTICLE_URL = "https://www.ohmynews.com/NWS_Web/View/at_pg.aspx?CNTN_CD="

SECTION_LABELS = {
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

OEUM_SECTIONS = {'M0111', 'M0113'}   # 오름 영역
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
    seen = {}
    for m in re.finditer(r'CNTN_CD=(A\d+)[^"\']*CMPT_CD=(M\d+)', html):
        cntn, cmpt = m.group(1), m.group(2)
        if cntn not in seen:
            seen[cntn] = {'cntn_cd': cntn, 'cmpt_cd': cmpt, 'rank': len(seen) + 1}

    titles = {}
    # M0111: class='p_tit' 링크에 제목
    for m in re.finditer(r"CNTN_CD=(A\d+)[^'\"]*CMPT_CD=M0111[^'\"]*['\"]?\s*class=['\"]p_tit['\"][^>]*>([^<]{5,100})</a>", html):
        cntn, title = m.group(1), m.group(2).strip()
        if cntn not in titles:
            titles[cntn] = title
    # M0113: <strong> 태그에 제목
    for m in re.finditer(r'CNTN_CD=(A\d+)[^>]*CMPT_CD=M0113[^>]*>.*?<strong>(.*?)</strong>', html, re.DOTALL):
        cntn = m.group(1)
        title = re.sub(r'<[^>]+>', ' ', m.group(2)).strip()
        title = re.sub(r'\s+', ' ', title)
        if cntn not in titles and title:
            titles[cntn] = title
    # M0112 이하: <a> 태그 텍스트
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
        result.append({
            'cntn_cd': cntn,
            'cmpt_cd': a['cmpt_cd'],
            'rank': a['rank'],
            'section': SECTION_LABELS.get(a['cmpt_cd'], a['cmpt_cd']),
            'is_oeum': a['cmpt_cd'] in OEUM_SECTIONS,
            'title': titles.get(cntn, ''),
            'author': authors.get(cntn, ''),
            'url': ARTICLE_URL + cntn,
        })
    return result


def fetch_slot(date_str, h):
    """단일 시간대 수집 → (time_key, articles) or (None, None)"""
    slot_id = f"{date_str}{h:02d}00"
    html = fetch_html(BASE_URL + slot_id)
    if html and len(re.findall(r'CNTN_CD=A\d+', html)) > 10:
        time_key = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {h:02d}:00"
        return time_key, extract_articles(html)
    return None, None


def collect_range(date_from_str, date_to_str):
    """날짜 범위 전체 병렬 수집"""
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


# ── 라이프사이클 분석 ───────────────────────────────────────────────

def compute_lifecycle(articles, timeslots):
    """오름 영역 기사별 라이프사이클 계산"""
    time_idx = {t: i for i, t in enumerate(timeslots)}

    # 기사별 메타 + 시간대별 상태 수집
    meta     = {}
    by_time  = defaultdict(dict)   # {cntn_cd: {time_key: article_row}}

    for a in articles:
        cntn = a['cntn_cd']
        by_time[cntn][a['time']] = a
        if cntn not in meta or (not meta[cntn]['title'] and a['title']):
            meta[cntn] = {'title': a['title'], 'author': a['author'], 'url': a['url']}

    # 오름에 한 번이라도 등장한 기사만
    oeum_cntns = {cntn for cntn, tm in by_time.items()
                  if any(r['cmpt_cd'] in OEUM_SECTIONS for r in tm.values())}

    STATE_ORDER = {'오름(메인)': 0, '오름(서브)': 1, '주요기사': 2}

    result = []
    for cntn in oeum_cntns:
        tm = by_time[cntn]

        # 등장한 전체 시간대의 상태 시퀀스
        stages = []
        for t in timeslots:
            if t in tm:
                r = tm[t]
                stages.append({
                    'time':    t,
                    'state':   r['section'],
                    'cmpt_cd': r['cmpt_cd'],
                    'is_oeum': r['is_oeum'],
                    'rank':    r['rank'],
                })

        oeum_stages = [s for s in stages if s['is_oeum']]
        if not oeum_stages:
            continue

        first_oeum = oeum_stages[0]['time']
        last_oeum  = oeum_stages[-1]['time']

        # 오름 이후 행방
        last_idx   = time_idx.get(last_oeum, -1)
        after_oeum = '퇴장'
        if last_idx >= 0:
            for t in timeslots[last_idx + 1:]:
                if t in tm:
                    after_oeum = tm[t]['section']
                    break

        # 오름 진입 전 이력 (오름 이전에 다른 섹션에 있었나)
        first_idx     = time_idx.get(first_oeum, 0)
        pre_oeum_sections = list({tm[t]['section'] for t in timeslots[:first_idx] if t in tm})

        result.append({
            'cntn_cd':          cntn,
            'title':            meta[cntn]['title'],
            'author':           meta[cntn]['author'],
            'url':              meta[cntn]['url'],
            'first_oeum':       first_oeum,
            'last_oeum':        last_oeum,
            'oeum_duration':    len(oeum_stages),
            'after_oeum':       after_oeum,
            'was_m0111':        any(s['cmpt_cd'] == 'M0111' for s in oeum_stages),
            'pre_oeum':         pre_oeum_sections,
            'stages':           stages,
        })

    # 오름 첫 진입 순서로 정렬
    result.sort(key=lambda x: time_idx.get(x['first_oeum'], 9999))
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

    # 최대 14일 제한
    d_from = datetime.strptime(date_from, '%Y%m%d')
    d_to   = datetime.strptime(date_to,   '%Y%m%d')
    if (d_to - d_from).days > 13:
        return jsonify({'error': '최대 14일 범위까지 분석 가능합니다'}), 400

    timeslots, articles = collect_range(date_from, date_to)

    if not timeslots:
        return jsonify({'error': '해당 기간의 데이터가 없습니다'}), 404

    lifecycle = compute_lifecycle(articles, timeslots)

    # 타임슬롯 표시용 레이블 (단일일 → "HH:00", 복수일 → "MM/DD HH:00")
    multi_day = date_from != date_to
    slot_labels = {}
    for t in timeslots:
        parts = t.split(' ')  # "2026-05-11 07:00"
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
