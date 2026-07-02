# -*- coding: utf-8 -*-
"""
트렌드 보드 (Trend Board)
- 전세계 인기 영상/밈을 한 페이지에 모아 보는 로컬 웹서버
- 자동 수집: YouTube 인기 급상승(공식 API) + Reddit 인기글(키 불필요)
- 정렬: 조회수 / 좋아요·업보트 / 댓글 / 종합점수
- 실행: python trend_server.py  →  브라우저에서 http://127.0.0.1:8770
"""

import json
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.parse
import html as html_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
HOST = "0.0.0.0"            # LAN 개방됨(팀원 공유용). 내 PC만 보려면 "127.0.0.1"
PORT = 8770
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_config.json")
CACHE_TTL = 20 * 60         # 캐시 유효시간(초). API 호출 아끼려고 20분 캐싱.
UA = "trend-board/1.0 (local research tool)"

# YouTube 인기 급상승을 가져올 국가 (regionCode)
YT_REGIONS = {
    "KR": "🇰🇷 한국",
    "US": "🇺🇸 미국",
    "JP": "🇯🇵 일본",
    "GB": "🇬🇧 영국",
    "BR": "🇧🇷 브라질",
    "IN": "🇮🇳 인도",
    "FR": "🇫🇷 프랑스",
    "DE": "🇩🇪 독일",
}

# Reddit 서브레딧 (카테고리별). OAuth 모드에서 전체 사용.
REDDIT_SUBS = [
    ("memes", "밈"),
    ("dankmemes", "밈"),
    ("funny", "유머"),
    ("videos", "영상"),
    ("TikTokCringe", "영상"),
    ("PublicFreakout", "영상"),
    ("nextfuckinglevel", "놀라움"),
    ("BeAmazed", "놀라움"),
    ("Damnthatsinteresting", "흥미로움"),
    ("interestingasfuck", "흥미로움"),
    ("oddlysatisfying", "만족감"),
    ("Unexpected", "반전"),
]

# 키가 하나도 없을 때 쓰는 무설정 폴백 (meme-api = Reddit 이미지밈 프록시).
# 이미지 위주 서브레딧만 지원, 댓글 수는 제공 안 됨.
MEME_FALLBACK_SUBS = [
    ("memes", "밈"),
    ("dankmemes", "밈"),
    ("wholesomememes", "밈"),
    ("me_irl", "밈"),
    ("funny", "유머"),
    ("reactiongifs", "움짤"),
    ("gifs", "움짤"),
    ("oddlysatisfying", "만족감"),
    ("photoshopbattles", "합성"),
    ("aww", "귀여움"),
    ("food", "음식"),
    ("pics", "사진"),
]

# 키 없이도 살아있는 Reddit RSS 경로 (top/day). meme-api가 못 주는 영상·반응 서브레딧용.
# RSS는 정확한 업보트/댓글 수는 안 주지만 "오늘 top" 순서라 인기순으로 나옴.
# (sub, 카테고리, 영상위주여부)
RSS_SUBS = [
    ("videos", "영상", True),
    ("nextfuckinglevel", "놀라움", True),
    ("PublicFreakout", "영상", True),
    ("BeAmazed", "놀라움", True),
    ("TikTokCringe", "영상", True),
]

# ─────────────────────────────────────────────────────────────
# 캐시
# ─────────────────────────────────────────────────────────────
_cache = {"data": None, "ts": 0}
_cache_lock = threading.Lock()
_rd_token = {"val": None, "exp": 0}
_rd_lock = threading.Lock()
_rss_by_sub = {}          # {sub: [items]} — 백그라운드 스레드가 천천히 채움
_rss_lock = threading.Lock()


def load_config():
    """trend_config.json 읽기 (utf-8-sig BOM 대응). 파일 없으면 환경변수로 폴백(클라우드용)."""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[경고] {CONFIG_FILE} 읽기 실패: {e}")
    # 환경변수 폴백 (GitHub Actions Secret 등). 파일 값이 있으면 그걸 우선.
    env_map = {
        "youtube_api_key": "YOUTUBE_API_KEY",
        "reddit_client_id": "REDDIT_CLIENT_ID",
        "reddit_client_secret": "REDDIT_CLIENT_SECRET",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
    }
    for k, env in env_map.items():
        if not cfg.get(k) and os.environ.get(env):
            cfg[k] = os.environ[env]
    return cfg


def _http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ─────────────────────────────────────────────────────────────
# 콘텐츠 종류 분류 (노래/애니/실사/게임/유머/기타) — 휴리스틱
# ─────────────────────────────────────────────────────────────
_KW_MUSIC = ["official video", "official mv", "official m/v", "[mv]", "(mv)", "m/v",
             "music video", "official audio", "lyric", "가사", "뮤직비디오", "뮤비",
             "feat.", "ft.", " ost", "ost)", "(ost", "cover)", "(cover", "무대",
             "audio)", "라이브클립", "라이브 클립", "song)"]
_KW_GAME = ["gameplay", "gaming", "playthrough", "speedrun", "minecraft", "roblox",
            "fortnite", "valorant", "발로란트", "리그오브레전드", "league of legends",
            "elden ring", "gta ", "배틀그라운드", "배그", "마인크래프트", "게임",
            "montage", "롤토체스", "стрим", "no commentary"]
_KW_ANIME = ["anime", "애니", "애니메이션", "animation", "animated", "cartoon", "만화",
             "amv", "opening theme", "ending theme", "manga", "webtoon", "웹툰",
             "1화", "2화", "3화", "화 무료", "OP full", "ed full"]
_KW_MEME = ["meme", "밈", "웃긴", "드립", "짤", "comedy", "개그", "shitpost", "cursed"]


def _has(text, kws):
    return any(k in text for k in kws)


def classify_ctype(item, yt_category_id=None):
    """콘텐츠 종류 태그 반환. 우선순위: 노래>게임>애니>유머>실사>기타."""
    title = (item.get("title") or "")
    tl = title.lower()
    cid = str(yt_category_id) if yt_category_id is not None else ""

    # 1) 노래/음악
    if cid == "10" or _has(tl, _KW_MUSIC):
        return "노래"
    # 2) 게임
    if cid == "20" or _has(tl, _KW_GAME):
        return "게임"
    # 3) 애니/만화
    if _has(tl, _KW_ANIME) or (cid == "1" and _has(tl, ["trailer", "teaser"]) is False and _has(tl, _KW_ANIME)):
        return "애니"
    # 4) 유머/밈
    cat = item.get("category") or ""
    if cid == "23" or cat in ("밈", "유머") or _has(tl, _KW_MEME):
        return "유머"
    # 5) 실사 (실제 영상/사진) — 기본값 (youtube/reddit/tvcf 모두)
    if item.get("source") in ("youtube", "reddit", "tvcf"):
        return "실사"
    return "기타"


# ─────────────────────────────────────────────────────────────
# YouTube
# ─────────────────────────────────────────────────────────────
def fetch_youtube_region(region, api_key, max_results=20):
    params = urllib.parse.urlencode({
        "part": "snippet,statistics",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": max_results,
        "key": api_key,
    })
    url = "https://www.googleapis.com/youtube/v3/videos?" + params
    out = []
    try:
        data = _http_get_json(url)
    except Exception as e:
        print(f"[YouTube {region}] 실패: {e}")
        return out
    for it in data.get("items", []):
        sn = it.get("snippet", {})
        st = it.get("statistics", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        vid = it.get("id", "")
        item = {
            "source": "youtube",
            "id": "yt_" + vid,
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "thumbnail": thumb,
            "link": f"https://www.youtube.com/watch?v={vid}",
            "contentUrl": f"https://www.youtube.com/embed/{vid}",
            "isVideo": True,
            "views": int(st.get("viewCount", 0) or 0),
            "likes": int(st.get("likeCount", 0) or 0),
            "comments": int(st.get("commentCount", 0) or 0),
            "region": YT_REGIONS.get(region, region),
            "category": "인기 급상승",
            "created": sn.get("publishedAt", ""),
        }
        item["ctype"] = classify_ctype(item, sn.get("categoryId"))
        out.append(item)
    return out


def fetch_youtube_all(api_key):
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_youtube_region, r, api_key) for r in YT_REGIONS]
        for f in as_completed(futs):
            results.extend(f.result())
    return results


# 니치 바이럴 포맷 탐색 키워드 (검색→조회수순). "인기 급상승"이 못 잡는 포맷들.
NICHE_QUERIES = [
    "miniature doll cooking asmr", "tiny kitchen cooking", "ai cooking asmr",
    "glass food asmr", "cooking asmr", "mukbang asmr", "oddly satisfying",
    "street food asmr", "ai video", "transformation", "diy miniature", "food asmr",
]
_niche_cache = {"data": [], "ts": 0}
_niche_lock = threading.Lock()
NICHE_TTL = 6 * 3600       # 검색은 쿼터 비싸서 6시간 캐시
NICHE_WINDOW_DAYS = 365    # 최근 1년 내 최고 조회수 = 그 포맷의 진짜 앵커 영상
NICHE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "niche_cache.json")


def _load_niche_disk():
    try:
        with open(NICHE_CACHE_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        if time.time() - j.get("ts", 0) < NICHE_TTL:
            return j.get("data"), j.get("ts", 0)
    except Exception:
        pass
    return None, 0


def _save_niche_disk(data, ts):
    try:
        with open(NICHE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "data": data}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[niche cache 저장] 실패: {e}")


def _yt_search_ids(query, api_key, after_iso, n=8):
    params = urllib.parse.urlencode({
        "part": "snippet", "q": query, "type": "video", "order": "viewCount",
        "publishedAfter": after_iso, "maxResults": n, "key": api_key,
    })
    try:
        data = _http_get_json("https://www.googleapis.com/youtube/v3/search?" + params, timeout=15)
    except Exception as e:
        print(f"[YT search '{query}'] 실패: {e}")
        return []
    return [(it["id"]["videoId"], query) for it in data.get("items", []) if it.get("id", {}).get("videoId")]


def fetch_youtube_niche(api_key):
    """니치 키워드로 조회수순 검색 → 통계 붙여 아이템화. 6시간 캐시."""
    now = time.time()
    with _niche_lock:
        if _niche_cache["data"] and (now - _niche_cache["ts"] < NICHE_TTL):
            return _niche_cache["data"]
    # 디스크 캐시(재시작해도 쿼터 안 씀)
    disk, disk_ts = _load_niche_disk()
    if disk is not None:
        with _niche_lock:
            _niche_cache["data"], _niche_cache["ts"] = disk, disk_ts
        return disk
    after_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - NICHE_WINDOW_DAYS * 86400))
    id_niche = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_yt_search_ids, q, api_key, after_iso) for q in NICHE_QUERIES]
        for f in as_completed(futs):
            for vid, q in f.result():
                id_niche.setdefault(vid, q)  # 여러 쿼리에 걸리면 첫 니치 유지
    ids = list(id_niche.keys())
    out = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = urllib.parse.urlencode({
            "part": "snippet,statistics", "id": ",".join(chunk), "key": api_key,
        })
        try:
            data = _http_get_json("https://www.googleapis.com/youtube/v3/videos?" + params, timeout=15)
        except Exception as e:
            print(f"[YT niche stats] 실패: {e}")
            continue
        for it in data.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            vid = it.get("id", "")
            item = {
                "source": "youtube",
                "id": "yt_" + vid,
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "thumbnail": thumb,
                "link": f"https://www.youtube.com/watch?v={vid}",
                "contentUrl": f"https://www.youtube.com/embed/{vid}",
                "isVideo": True,
                "views": int(st.get("viewCount", 0) or 0),
                "likes": int(st.get("likeCount", 0) or 0),
                "comments": int(st.get("commentCount", 0) or 0),
                "region": "🔎 " + id_niche.get(vid, "니치"),
                "category": "니치 트렌드",
                "niche": id_niche.get(vid, ""),
                "created": sn.get("publishedAt", ""),
            }
            item["ctype"] = classify_ctype(item, sn.get("categoryId"))
            out.append(item)
    with _niche_lock:
        _niche_cache["data"] = out
        _niche_cache["ts"] = now
    _save_niche_disk(out, now)
    return out


# ─────────────────────────────────────────────────────────────
# TVCF (해외 광고 아카이브) — SSR HTML 파싱, 조회수 없음(최신순)
# ─────────────────────────────────────────────────────────────
TVCF_ROWS = 150   # 한 번에 가져올 해외 광고 개수 (rows). 늘리면 그만큼 더 가져옴.
TVCF_URL = (f"https://tvcf.co.kr/worked/video?mediaType_value=1&page=1&rows={TVCF_ROWS}"
            "&sort_by=registrated_date&country_code_value=410&lang=ko&exclude_country_code=true")
_tvcf_cache = {"data": [], "ts": 0}
_tvcf_lock = threading.Lock()
TVCF_TTL = 3600  # 최신 광고라 1시간 캐시


def fetch_tvcf():
    now = time.time()
    with _tvcf_lock:
        if _tvcf_cache["data"] and (now - _tvcf_cache["ts"] < TVCF_TTL):
            return _tvcf_cache["data"]
    try:
        req = urllib.request.Request(TVCF_URL, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[TVCF] 실패: {e}")
        return []
    thumbs = {}
    for mid, src in re.findall(
            r'href="/play/([a-z0-9-]+)">\s*<div[^>]*>\s*<img[^>]*?src="(https://nmedia[^"]+)"', raw, re.S):
        thumbs.setdefault(mid, src)
    out, seen = [], set()
    for m in re.finditer(r'href="/play/([a-z0-9-]+)">', raw):
        mid = m.group(1)
        chunk = raw[m.end():m.end() + 300]
        toks = [html_lib.unescape(t).strip() for t in re.split(r"<[^>]+>", chunk) if t.strip()]
        if len(toks) < 2 or '"' in toks[0] or "=" in toks[0] or mid in seen:
            continue
        seen.add(mid)
        advertiser, title = toks[0], toks[1]
        item = {
            "source": "tvcf",
            "id": "tv_" + mid,
            "title": title,
            "channel": advertiser,
            "thumbnail": thumbs.get(mid, ""),
            "link": "https://tvcf.co.kr/play/" + mid,
            "contentUrl": "https://tvcf.co.kr/play/" + mid,
            "isVideo": True,
            "views": 0, "likes": 0, "comments": 0,
            "no_metrics": True,
            "rank": len(out),
            "region": "📺 해외광고",
            "category": "해외 광고",
            "created": 0,
        }
        item["ctype"] = classify_ctype(item)
        out.append(item)
    with _tvcf_lock:
        _tvcf_cache["data"] = out
        _tvcf_cache["ts"] = now
    return out


# ─────────────────────────────────────────────────────────────
# Reddit
# ─────────────────────────────────────────────────────────────
def _reddit_thumb(d):
    # preview 원본 이미지 우선, 없으면 thumbnail
    try:
        imgs = d["preview"]["images"][0]
        src = imgs.get("source", {}).get("url", "")
        if src:
            return html_lib.unescape(src)
    except Exception:
        pass
    t = d.get("thumbnail", "")
    if t and t.startswith("http"):
        return t
    return ""


def _reddit_token(cfg):
    """공식 OAuth application-only 토큰 (1시간 캐시). 앱 키 없으면 None."""
    cid = cfg.get("reddit_client_id", "").strip()
    csec = cfg.get("reddit_client_secret", "").strip()
    if not cid or not csec:
        return None
    now = time.time()
    with _rd_lock:
        if _rd_token["val"] and now < _rd_token["exp"] - 60:
            return _rd_token["val"]
    import base64
    auth = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=data,
        headers={"Authorization": "Basic " + auth, "User-Agent": UA},
    )
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
        tok = d.get("access_token")
        with _rd_lock:
            _rd_token["val"] = tok
            _rd_token["exp"] = now + int(d.get("expires_in", 3600))
        return tok
    except Exception as e:
        print(f"[Reddit OAuth] 토큰 발급 실패: {e}")
        return None


def fetch_reddit_oauth(sub, cat, token, t="day", limit=15):
    url = f"https://oauth.reddit.com/r/{sub}/top?" + urllib.parse.urlencode({"t": t, "limit": limit})
    out = []
    req = urllib.request.Request(url, headers={"Authorization": "bearer " + token, "User-Agent": UA})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[Reddit r/{sub}] 실패: {e}")
        return out
    for ch in data.get("data", {}).get("children", []):
        d = ch.get("data", {})
        if d.get("over_18") or d.get("stickied"):
            continue
        thumb = _reddit_thumb(d)
        if not thumb:
            continue
        item = {
            "source": "reddit",
            "id": "rd_" + d.get("id", ""),
            "title": d.get("title", ""),
            "channel": "r/" + sub,
            "thumbnail": thumb,
            "link": "https://www.reddit.com" + d.get("permalink", ""),
            "contentUrl": d.get("url", ""),
            "isVideo": bool(d.get("is_video") or d.get("post_hint") == "hosted:video"),
            "views": 0,
            "likes": int(d.get("ups", 0) or 0),
            "comments": int(d.get("num_comments", 0) or 0),
            "region": "🌐 글로벌",
            "category": cat,
            "created": int(d.get("created_utc", 0) or 0),
        }
        item["ctype"] = classify_ctype(item)
        out.append(item)
    return out


def fetch_reddit_fallback(sub, cat, count=12):
    """무설정 폴백: meme-api(Reddit 이미지밈 프록시). 댓글 수 없음."""
    url = f"https://meme-api.com/gimme/{sub}/{count}"
    out = []
    try:
        data = _http_get_json(url)
    except Exception as e:
        print(f"[meme-api r/{sub}] 실패: {e}")
        return out
    for m in data.get("memes", []):
        if m.get("nsfw") or m.get("spoiler"):
            continue
        prev = m.get("preview") or []
        thumb = prev[-1] if prev else m.get("url", "")
        item = {
            "source": "reddit",
            "id": "rd_" + (m.get("postLink", "").rstrip("/").split("/")[-1] or m.get("title", "")[:8]),
            "title": m.get("title", ""),
            "channel": "r/" + m.get("subreddit", sub),
            "thumbnail": thumb,
            "link": m.get("postLink", ""),
            "contentUrl": m.get("url", ""),
            "isVideo": False,
            "views": 0,
            "likes": int(m.get("ups", 0) or 0),
            "comments": 0,
            "region": "🌐 글로벌",
            "category": cat,
            "created": 0,
        }
        item["ctype"] = classify_ctype(item)
        out.append(item)
    return out


_RSS_UA = "Mozilla/5.0 (compatible; TrendBoardFeedreader/1.0)"


def fetch_reddit_rss(sub, cat, is_video):
    """키 없이 살아있는 Reddit RSS(top/day). 정확한 수치는 없지만 인기순으로 나옴."""
    url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit=15"
    out = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _RSS_UA})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[Reddit RSS r/{sub}] 실패: {e}")
        return out
    entries = re.findall(r"<entry>(.*?)</entry>", raw, re.S)
    for idx, e in enumerate(entries):
        mt = re.search(r"<title>(.*?)</title>", e, re.S)
        ml = re.search(r'<link href="(.*?)"', e)
        mc = re.search(r'<content type="html">(.*?)</content>', e, re.S)
        mid = re.search(r"<id>(.*?)</id>", e)
        title = html_lib.unescape(mt.group(1)).strip() if mt else ""
        link = html_lib.unescape(ml.group(1)) if ml else ""
        content = html_lib.unescape(mc.group(1)) if mc else ""
        imgs = re.findall(r'<img src="(.*?)"', content)
        thumb = html_lib.unescape(imgs[0]) if imgs else ""
        if not title or not thumb:
            continue
        pid = (mid.group(1).split("/")[-1] if mid else "") or f"{sub}{idx}"
        item = {
            "source": "reddit",
            "id": "rd_" + pid,
            "title": title,
            "channel": "r/" + sub,
            "thumbnail": thumb,
            "link": link,
            "contentUrl": link,
            "isVideo": is_video,
            "views": 0,
            "likes": 0,
            "comments": 0,
            "no_metrics": True,   # RSS는 정확한 수치 없음 → rank로 정렬
            "rank": idx,
            "region": "🌐 글로벌",
            "category": cat,
            "created": 0,
        }
        item["ctype"] = classify_ctype(item)
        out.append(item)
    return out


def fetch_reddit_all(cfg, t="day"):
    token = _reddit_token(cfg)
    results = []
    if token:
        mode = "oauth"
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_reddit_oauth, sub, cat, token, t) for sub, cat in REDDIT_SUBS]
            for f in as_completed(futs):
                results.extend(f.result())
    else:
        mode = "fallback"
        # 1) meme-api: 이미지 밈(업보트 수치 O) — 병렬
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_reddit_fallback, sub, cat) for sub, cat in MEME_FALLBACK_SUBS]
            for f in as_completed(futs):
                results.extend(f.result())
        # 2) RSS: 백그라운드 스레드가 천천히 모아둔 결과를 그대로 읽음(요청 안 막힘)
        with _rss_lock:
            for rows in _rss_by_sub.values():
                results.extend(rows)
    return results, mode


def rss_refresh_loop(interval=40):
    """Reddit RSS는 IP당 ~40초에 1개만 허용 → 백그라운드에서 천천히 순환 수집.
    5개 서브 기준 각 서브는 약 3~4분마다 갱신됨. 요청 처리를 막지 않음."""
    i = 0
    while True:
        if RSS_SUBS:
            sub, cat, is_video = RSS_SUBS[i % len(RSS_SUBS)]
            try:
                rows = fetch_reddit_rss(sub, cat, is_video)
                if rows:
                    with _rss_lock:
                        _rss_by_sub[sub] = rows
            except Exception as e:
                print(f"[RSS loop r/{sub}] {e}")
            i += 1
        time.sleep(interval)


# ─────────────────────────────────────────────────────────────
# 트렌드 클러스터 (제목에서 공통 주제 자동 추출)
# ─────────────────────────────────────────────────────────────
_STOP = set("""
official video audio lyric lyrics mv m/v ft feat full hd 4k live new the a an of and or to in on
for with at by from is are be my your you it this that how what why we out up now vs ft. feat.
episode ep part vol season teaser trailer clip shorts short reaction review 그리고 그는 하는 하면
""".split())


def _norm_tokens(title):
    t = (title or "").lower()
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return [w for w in t.split() if w not in _STOP and len(w) >= 2 and not w.isdigit()]


def build_clusters(items, top_n=12, min_size=3):
    """제목의 2~3단어 공통구절로 인기 영상들을 주제별 클러스터로 묶는다."""
    from collections import defaultdict
    docs = []
    for it in items:
        toks = _norm_tokens(it.get("title"))
        phrases = set()
        for n in (3, 2):
            for i in range(len(toks) - n + 1):
                phrases.add(" ".join(toks[i:i + n]))
        docs.append(phrases)
    df = defaultdict(list)
    for idx, phrases in enumerate(docs):
        for p in phrases:
            df[p].append(idx)

    def seed_weight(idxs):
        # 조회수 합(레딧은 최소가중) — 얼마나 크게 터진 주제인가
        return sum(max(items[i].get("views", 0), 3000) for i in idxs)

    seeds = [(p, idxs) for p, idxs in df.items() if len(idxs) >= min_size]
    seeds.sort(key=lambda x: (seed_weight(x[1]), len(x[1])), reverse=True)

    assigned, clusters = set(), []
    for p, idxs in seeds:
        members = [i for i in idxs if i not in assigned]
        if len(members) < min_size:
            continue
        for i in members:
            assigned.add(i)
        vids = sorted((items[i] for i in members), key=lambda x: x.get("views", 0), reverse=True)
        clusters.append({
            "label": p,
            "count": len(vids),
            "total_views": sum(v.get("views", 0) for v in vids),
            "ai_name": "",
            "ai_desc": "",
            "rating": 0,
            "videos": [{
                "title": v.get("title", ""), "link": v.get("link", ""),
                "thumbnail": v.get("thumbnail", ""), "views": v.get("views", 0),
                "channel": v.get("channel", ""), "source": v.get("source", ""),
                "no_metrics": v.get("no_metrics", False),
            } for v in vids[:8]],
        })
        if len(clusters) >= top_n:
            break
    clusters.sort(key=lambda c: (c["total_views"], c["count"]), reverse=True)
    return clusters


# Claude(나)가 미리 분석해둔 바이럴 포맷 사전 — API 키 없이도 한글명·설명·별점 제공.
# (키워드 리스트, 한글명, 설명, 별점). 구체적인 것부터 위로(먼저 매칭).
TREND_LABELS = [
    (["miniature doll", "tiny kitchen", "miniature cooking", "mini kitchen", "미니어처"],
     "AI 미니어처 인형 요리", "손바닥만 한 미니 부엌에서 인형이 진짜 요리하는 ASMR. 인도·동남아발 글로벌 점령.", 3),
    (["glass food", "glass fruit", "made of glass", "edible glass", "glass spicy",
      "glass sandwich", "ai glass", "glass "],
     "글래스 푸드 ASMR", "유리로 만든 음식을 자르는 초현실 커팅 ASMR. 비주얼·사운드 극대화.", 3),
    (["ai cooking", "hyperreal", "ai food", "ai recipe", "ai kitchen", "ai chef"],
     "AI 하이퍼리얼 요리 ASMR", "AI로 만든 초현실 요리 과정. 불·기름·칼질 사운드를 극대화한 숏폼.", 3),
    (["ai cat", "cat ai", "ai animal", "ai pet", "cat drama", "kitten"],
     "AI 동물 감성 드라마", "AI로 만든 고양이·동물 감정 스토리. 짧고 자극적이라 숏폼에서 급확산.", 3),
    (["mukbang", "먹방"],
     "먹방 ASMR", "과장된 소리·비주얼의 먹방. 국경 없는 스테디셀러 포맷.", 2),
    (["street food"],
     "길거리 음식 클로즈업", "대형 조리 과정을 근접 촬영. 식욕+지역색으로 조회수 폭발.", 2),
    (["slime", "슬라임"],
     "슬라임 ASMR", "촉각을 자극하는 슬라임 플레이. 키즈·힐링 타깃.", 2),
    (["oddly satisfying", "satisfying", "만족"],
     "오들리 새티스파잉", "반복·정밀·완성의 쾌감을 주는 무음/ASMR 영상. 몰입도 높음.", 2),
    (["makeup", "transformation", "glow up", "메이크업", "변신"],
     "메이크업 변신", "비포·애프터 극적 변화. 뷰티 광고 레퍼런스로 강력.", 2),
    (["science", "experiment", "project", "실험"],
     "사이언스 실험 숏폼", "폭발·화학반응 등 눈길 끄는 과학 실험. 호기심 유발.", 2),
    (["diy", "craft", "공예"],
     "DIY·공예 과정", "작고 정교한 제작 과정. 만족감+따라 하기 수요.", 2),
    (["challenge", "챌린지"],
     "참여형 챌린지", "따라 하기 쉬운 참여형 포맷. 확산성 높음.", 2),
    (["prank"],
     "몰카·프랭크", "반응을 노린 상황극. 숏폼 바이럴 단골.", 1),
    (["asmr", "food"],
     "푸드 ASMR", "먹고 조리하는 소리로 몰입시키는 ASMR 포맷.", 2),
    (["trailer", "teaser", "official", "annonce"],
     "영화·예고편", "대작 예고편·티저. 개봉/공개 프로모션 트렌드.", 1),
    (["gameplay", "chameleon", "게임"],
     "게임 하이라이트·밈", "인기 게임 플레이·밈. 커뮤니티 중심 확산.", 1),
]


def annotate_clusters_local(clusters):
    """API 키 없이, 미리 큐레이션한 TREND_LABELS로 한글명·설명·별점 매칭."""
    for c in clusters:
        hay = (c.get("label", "") + " " + " ".join(
            v.get("title", "") for v in c.get("videos", [])[:5])).lower()
        for kws, name, desc, rating in TREND_LABELS:
            if any(k in hay for k in kws):
                c["ai_name"], c["ai_desc"], c["rating"] = name, desc, rating
                break


def ai_annotate_clusters(clusters, api_key):
    """Claude로 각 클러스터에 한글 이름·설명·별점 생성 (키 있을 때만)."""
    if not clusters or not api_key:
        return
    lines = []
    for i, c in enumerate(clusters):
        titles = " / ".join(v["title"][:60] for v in c["videos"][:4])
        lines.append(f'{i}. [{c["label"]}] 조회수합 {c["total_views"]:,} · 영상{c["count"]} · 예: {titles}')
    prompt = (
        "너는 광고 콘텐츠 기획자를 돕는 트렌드 분석가야. 아래는 지금 전세계에서 인기인 영상들을 "
        "제목 공통구절로 묶은 클러스터 목록이야. 각 클러스터에 대해 광고 레퍼런스 관점에서 한국어로 "
        "① 눈에 띄는 한글 트렌드명(ai_name, 12자 내외) ② 한 줄 설명(desc, 40자 내외, 어떤 포맷/왜 뜨는지) "
        "③ 광고 활용도 별점(rating, 1~3 정수)을 매겨줘.\n\n"
        + "\n".join(lines)
        + '\n\n반드시 순수 JSON 배열만 출력. 형식: '
          '[{"i":0,"ai_name":"...","desc":"...","rating":3}, ...]'
    )
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
        text = resp["content"][0]["text"]
        m = re.search(r"\[.*\]", text, re.S)
        arr = json.loads(m.group(0) if m else text)
        for a in arr:
            i = a.get("i")
            if isinstance(i, int) and 0 <= i < len(clusters):
                clusters[i]["ai_name"] = str(a.get("ai_name", ""))[:40]
                clusters[i]["ai_desc"] = str(a.get("desc", ""))[:120]
                clusters[i]["rating"] = int(a.get("rating", 0) or 0)
    except Exception as e:
        print(f"[AI 클러스터 주석] 실패: {e}")


# ─────────────────────────────────────────────────────────────
# 집계 + 캐시
# ─────────────────────────────────────────────────────────────
def build_trends(force=False, t="day"):
    now = time.time()
    with _cache_lock:
        if not force and _cache["data"] and (now - _cache["ts"] < CACHE_TTL):
            return _cache["data"]

    cfg = load_config()
    api_key = cfg.get("youtube_api_key", "").strip()

    items = []
    yt_ok = False
    if api_key:
        yt = fetch_youtube_all(api_key)
        items.extend(yt)
        yt_ok = len(yt) > 0
        items.extend(fetch_youtube_niche(api_key))  # 니치 바이럴 포맷(검색, 6h캐시)
    reddit, reddit_mode = fetch_reddit_all(cfg, t)
    items.extend(reddit)
    items.extend(fetch_tvcf())  # TVCF 해외 광고 아카이브(최신순, 1h캐시)

    # 중복 제거: 같은 영상이 여러 나라 인기목록에 잡히면 1개로 합치고 "N개국" 표시
    merged, order = {}, []
    for it in items:
        k = it.get("id")
        if k in merged:
            r = it.get("region")
            if r and r not in merged[k]["_regions"]:
                merged[k]["_regions"].append(r)
            continue
        it["_regions"] = [it.get("region")] if it.get("region") else []
        merged[k] = it
        order.append(k)
    items = [merged[k] for k in order]
    for it in items:
        rs = it.pop("_regions", [])
        it["regions_count"] = len(rs) or 1
        if it["source"] == "youtube" and len(rs) > 1:
            it["region"] = f"🌍 {len(rs)}개국 인기"

    # 종합 점수: 소스별 최대값으로 정규화(0~1) → 공정한 혼합 정렬
    def metric(it):
        if it["source"] == "youtube":
            return it["views"] + it["likes"] * 20 + it["comments"] * 50
        return it["likes"] + it["comments"] * 15  # reddit

    # 수치가 있는 항목만 정규화, 수치 없는 RSS 항목은 순위(rank)로 점수 부여
    max_by_src = {}
    for it in items:
        if it.get("no_metrics"):
            continue
        m = metric(it)
        it["_metric"] = m
        max_by_src[it["source"]] = max(max_by_src.get(it["source"], 1), m)
    for it in items:
        if it.get("no_metrics"):
            # top/day 순서: 1위≈0.8에서 서서히 하락, 최소 0.25 → 종합 인기에서 상위 노출
            it["score"] = round(max(0.25, 0.8 - it.get("rank", 0) * 0.035), 4)
            continue
        mx = max_by_src.get(it["source"], 1) or 1
        it["score"] = round(it["_metric"] / mx, 4)
        del it["_metric"]

    # 트렌드 클러스터 (제목 공통구절로 주제별 묶기)
    clusters = build_clusters(items)
    annotate_clusters_local(clusters)  # Claude 큐레이션 사전(키 불필요)
    anthropic_key = cfg.get("anthropic_api_key", "").strip()
    if anthropic_key:  # 키 있으면 실시간 AI가 덮어씀(더 정교)
        ai_annotate_clusters(clusters, anthropic_key)

    payload = {
        "updated": int(now),
        "count": len(items),
        "youtube_enabled": bool(api_key),
        "youtube_ok": yt_ok,
        "reddit_mode": reddit_mode,  # "oauth" | "fallback"
        "ai_clusters": bool(anthropic_key),
        "clusters": clusters,
        "items": items,
    }
    with _cache_lock:
        _cache["data"] = payload
        _cache["ts"] = now
    return payload


# ─────────────────────────────────────────────────────────────
# HTTP 핸들러
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 조용히

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._send(200, PAGE_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/trends":
            qs = urllib.parse.parse_qs(parsed.query)
            force = qs.get("refresh", ["0"])[0] == "1"
            t = qs.get("t", ["day"])[0]
            if t not in ("day", "week"):
                t = "day"
            try:
                data = build_trends(force=force, t=t)
                self._send(200, json.dumps(data, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        self._send(404, json.dumps({"error": "not found"}))


# ─────────────────────────────────────────────────────────────
# 프론트엔드 (정적 HTML — 데이터는 /api/trends 로 fetch)
# ─────────────────────────────────────────────────────────────
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🔥 트렌드 보드 — 전세계 인기 영상·밈</title>
<style>
  :root { --bg:#0e0f13; --card:#181a20; --card2:#1f222b; --line:#2a2e39; --tx:#e8eaf0; --sub:#9aa0ad; --accent:#ff5a5f; --yt:#ff0033; --rd:#ff4500; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--tx); font-family:-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif; }
  header { position:sticky; top:0; z-index:10; background:rgba(14,15,19,.92); backdrop-filter:blur(8px); border-bottom:1px solid var(--line); padding:14px 20px; }
  .title { font-size:20px; font-weight:800; letter-spacing:-.3px; }
  .title small { color:var(--sub); font-weight:500; font-size:12px; margin-left:8px; }
  .bar { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; align-items:center; }
  .seg { display:flex; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .seg button { background:transparent; color:var(--sub); border:0; padding:7px 13px; font-size:13px; cursor:pointer; font-weight:600; }
  .seg button.on { background:var(--accent); color:#fff; }
  .spacer { flex:1; }
  .meta { color:var(--sub); font-size:12px; }
  .refresh { background:var(--card2); color:var(--tx); border:1px solid var(--line); border-radius:10px; padding:7px 13px; font-size:13px; cursor:pointer; font-weight:600; }
  .refresh:hover { border-color:var(--accent); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:16px; padding:20px; max-width:1600px; margin:0 auto; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden; display:flex; flex-direction:column; transition:transform .12s, border-color .12s; }
  .card:hover { transform:translateY(-3px); border-color:#3a3f4d; }
  .thumbwrap { position:relative; aspect-ratio:16/9; background:#000; overflow:hidden; }
  .thumbwrap img { width:100%; height:100%; object-fit:cover; display:block; }
  .badge { position:absolute; top:8px; left:8px; font-size:11px; font-weight:700; padding:3px 8px; border-radius:6px; color:#fff; }
  .b-yt { background:var(--yt); } .b-rd { background:var(--rd); } .b-tv { background:#6c5ce7; }
  .play { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:44px; color:#fff; opacity:.85; text-shadow:0 2px 8px rgba(0,0,0,.6); pointer-events:none; }
  .region { position:absolute; bottom:8px; right:8px; background:rgba(0,0,0,.65); color:#fff; font-size:11px; padding:2px 7px; border-radius:6px; }
  .body { padding:11px 12px 12px; display:flex; flex-direction:column; gap:8px; flex:1; }
  .ttl { font-size:14px; font-weight:600; line-height:1.35; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .ch { color:var(--sub); font-size:12px; }
  .stats { display:flex; gap:12px; color:var(--sub); font-size:12px; margin-top:auto; padding-top:4px; border-top:1px solid var(--line); }
  .stats span { display:flex; align-items:center; gap:3px; }
  .cat { display:inline-block; font-size:10px; color:var(--sub); border:1px solid var(--line); border-radius:5px; padding:1px 6px; }
  a.card { text-decoration:none; color:inherit; }
  .empty { text-align:center; color:var(--sub); padding:80px 20px; }
  .note { background:#1a1408; border:1px solid #4a3a10; color:#e8c874; font-size:13px; padding:10px 14px; border-radius:10px; margin:16px 20px 0; }
  .loading { text-align:center; padding:80px; color:var(--sub); }
  .spin { display:inline-block; width:26px; height:26px; border:3px solid var(--line); border-top-color:var(--accent); border-radius:50%; animation:sp .8s linear infinite; }
  @keyframes sp { to { transform:rotate(360deg); } }
  /* 클러스터 뷰 */
  .clusters { max-width:1400px; margin:0 auto; padding:20px; display:flex; flex-direction:column; gap:18px; }
  .ccard { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px 18px; }
  .chead { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .crank { font-size:15px; font-weight:800; color:var(--accent); }
  .cname { font-size:18px; font-weight:800; letter-spacing:-.3px; }
  .cstars { color:#ffcf4a; font-size:14px; }
  .cmeta { color:var(--sub); font-size:13px; margin-left:auto; }
  .cdesc { color:#c7ccd8; font-size:13px; margin:7px 0 2px; }
  .ctag { color:var(--sub); font-size:12px; }
  .cvids { display:flex; gap:12px; overflow-x:auto; padding:12px 2px 4px; }
  .cvid { flex:0 0 200px; text-decoration:none; color:inherit; }
  .cvid .vt { position:relative; aspect-ratio:16/9; border-radius:10px; overflow:hidden; background:#000; }
  .cvid .vt img { width:100%; height:100%; object-fit:cover; }
  .cvid .vv { position:absolute; bottom:6px; right:6px; background:rgba(0,0,0,.75); color:#fff; font-size:11px; padding:2px 6px; border-radius:5px; }
  .cvid .vtt { font-size:12px; margin-top:6px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .cvid .vc { font-size:11px; color:var(--sub); margin-top:2px; }
</style>
</head>
<body>
<header>
  <div class="title">🔥 트렌드 보드 <small>전세계 인기 영상·밈 · 광고 레퍼런스용</small></div>
  <div class="bar">
    <div class="seg" id="view">
      <button data-v="cluster" class="on">🧩 트렌드 클러스터</button>
      <button data-v="grid">📋 전체 그리드</button>
    </div>
    <div class="seg" id="ctype">
      <button data-v="all" class="on">전체종류</button>
      <button data-v="노래">🎵 노래</button>
      <button data-v="애니">🌸 애니</button>
      <button data-v="실사">🎬 실사</button>
      <button data-v="게임">🎮 게임</button>
      <button data-v="유머">😂 유머</button>
    </div>
    <div class="seg" id="src">
      <button data-v="all" class="on">전체</button>
      <button data-v="youtube">YouTube</button>
      <button data-v="reddit">Reddit</button>
      <button data-v="tvcf">📺 해외광고</button>
    </div>
    <div class="seg" id="sort">
      <button data-v="score" class="on">종합 인기</button>
      <button data-v="views">조회수</button>
      <button data-v="likes">좋아요</button>
      <button data-v="comments">댓글</button>
    </div>
    <div class="seg" id="time">
      <button data-v="day" class="on">오늘</button>
      <button data-v="week">이번주</button>
    </div>
    <div class="spacer"></div>
    <span class="meta" id="meta"></span>
    <button class="refresh" id="refresh">↻ 새로고침</button>
  </div>
</header>
<div id="note"></div>
<div id="content"><div class="loading"><div class="spin"></div><div style="margin-top:14px">불러오는 중…</div></div></div>

<script>
var STATE = { view:"cluster", ctype:"all", src:"all", sort:"score", time:"day", items:[], clusters:[] };
var CTYPE_ICON = { "노래":"🎵", "애니":"🌸", "실사":"🎬", "게임":"🎮", "유머":"😂", "기타":"📦" };

function fmt(n){
  if(!n) return "0";
  if(n>=1e8) return (n/1e8).toFixed(1)+"억";
  if(n>=1e4) return (n/1e4).toFixed(1)+"만";
  if(n>=1e3) return (n/1e3).toFixed(1)+"천";
  return String(n);
}
function esc(s){ var d=document.createElement("div"); d.textContent=s||""; return d.innerHTML; }

function renderClusters(){
  var c = document.getElementById("content");
  var cl = STATE.clusters||[];
  if(!cl.length){ c.innerHTML='<div class="empty">아직 클러스터가 없어요. 새로고침을 눌러보세요.<br><span style="font-size:12px">(니치 검색은 6시간마다 갱신)</span></div>'; return; }
  var h = '<div class="clusters">';
  for(var i=0;i<cl.length;i++){
    var g = cl[i];
    var name = g.ai_name || g.label.replace(/\b\w/g,function(m){return m.toUpperCase();});
    var stars = g.rating>0 ? '<span class="cstars">'+'★'.repeat(g.rating)+'☆'.repeat(3-g.rating)+'</span>' : '';
    var desc = g.ai_desc ? '<div class="cdesc">'+esc(g.ai_desc)+'</div>' : '';
    var vids='';
    for(var j=0;j<g.videos.length;j++){
      var v=g.videos[j];
      var vv = v.no_metrics ? '🔥 인기' : (v.source==="youtube"?'👁 '+fmt(v.views):'👍 '+fmt(v.likes));
      var th = v.thumbnail?'<img loading="lazy" src="'+esc(v.thumbnail)+'" onerror="this.style.display=\'none\'">':'';
      vids += '<a class="cvid" href="'+esc(v.link)+'" target="_blank" rel="noopener">'
            +   '<div class="vt">'+th+'<span class="vv">'+vv+'</span></div>'
            +   '<div class="vtt">'+esc(v.title)+'</div><div class="vc">'+esc(v.channel)+'</div>'
            + '</a>';
    }
    h += '<div class="ccard">'
       +   '<div class="chead"><span class="crank">#'+(i+1)+'</span><span class="cname">'+esc(name)+'</span>'+stars
       +     '<span class="cmeta">합산 👁 '+fmt(g.total_views)+' · 영상 '+g.count+'개 · <span class="ctag">'+esc(g.label)+'</span></span></div>'
       +   desc
       +   '<div class="cvids">'+vids+'</div>'
       + '</div>';
  }
  h += '</div>';
  c.innerHTML = h;
}

function render(){
  if(STATE.view==="cluster"){ renderClusters(); return; }
  var items = STATE.items.slice();
  if(STATE.src!=="all") items = items.filter(function(i){ return i.source===STATE.src; });
  if(STATE.ctype!=="all") items = items.filter(function(i){ return i.ctype===STATE.ctype; });
  var key = STATE.sort;
  items.sort(function(a,b){ return (b[key]||0)-(a[key]||0); });

  var c = document.getElementById("content");
  if(!items.length){ c.innerHTML='<div class="empty">표시할 콘텐츠가 없어요. 새로고침을 눌러보세요.</div>'; return; }

  var h = '<div class="grid">';
  for(var i=0;i<items.length;i++){
    var it = items[i];
    var badge = it.source==="youtube" ? '<span class="badge b-yt">YouTube</span>'
              : it.source==="tvcf" ? '<span class="badge b-tv">해외광고</span>'
              : '<span class="badge b-rd">Reddit</span>';
    var play = it.isVideo ? '<div class="play">▶</div>' : '';
    var stats = "";
    if(it.no_metrics){
      stats = '<span>🔥 오늘 인기</span>';
    } else {
      if(it.source==="youtube") stats += '<span>👁 '+fmt(it.views)+'</span>';
      stats += '<span>👍 '+fmt(it.likes)+'</span><span>💬 '+fmt(it.comments)+'</span>';
    }
    var thumb = it.thumbnail ? '<img loading="lazy" src="'+esc(it.thumbnail)+'" onerror="this.style.display=\'none\'">' : '';
    h += '<a class="card" href="'+esc(it.link)+'" target="_blank" rel="noopener">'
       +   '<div class="thumbwrap">'+thumb+badge+play+'<span class="region">'+esc(it.region)+'</span></div>'
       +   '<div class="body">'
       +     '<div class="ttl">'+esc(it.title)+'</div>'
       +     '<div class="ch">'+esc(it.channel)+' · <span class="cat">'+(CTYPE_ICON[it.ctype]||"")+' '+esc(it.ctype||it.category)+'</span></div>'
       +     '<div class="stats">'+stats+'</div>'
       +   '</div>'
       + '</a>';
  }
  h += '</div>';
  c.innerHTML = h;
}

function load(force){
  var c = document.getElementById("content");
  c.innerHTML = '<div class="loading"><div class="spin"></div><div style="margin-top:14px">불러오는 중… (첫 로드 최대 25초)</div></div>';
  var u = "/api/trends?t="+STATE.time+(force?"&refresh=1":"");
  fetch(u).then(function(r){ return r.json(); }).then(function(d){
    STATE.items = d.items||[];
    STATE.clusters = d.clusters||[];
    var dt = new Date((d.updated||0)*1000);
    document.getElementById("meta").textContent = d.count+"개 · "+(STATE.clusters.length)+"클러스터 · "+dt.toLocaleTimeString("ko-KR");
    var note = document.getElementById("note");
    var msgs = [];
    if(!d.youtube_enabled){
      msgs.push('⚙️ <b>YouTube 꺼짐</b> — trend_config.json에 무료 API 키를 넣으면 국가별 인기 급상승 영상이 추가됩니다.');
    } else if(!d.youtube_ok){
      msgs.push('⚠️ <b>YouTube 응답 없음</b> — API 키/할당량을 확인하세요.');
    }
    if(d.reddit_mode==="fallback"){
      msgs.push('⚙️ <b>Reddit 무료모드</b> — 밈(업보트) + 영상/반응글(🔥 오늘 인기순, RSS)로 표시 중. 정확한 댓글수는 Reddit이 막아둬 미표시.');
    }
    note.innerHTML = msgs.length ? '<div class="note">'+msgs.join('<br>')+'</div>' : '';
    render();
  }).catch(function(e){
    c.innerHTML = '<div class="empty">불러오기 실패: '+esc(String(e))+'</div>';
  });
}

function setSegOn(id, val){
  var box = document.getElementById(id);
  Array.prototype.forEach.call(box.children, function(x){
    x.classList.toggle("on", x.getAttribute("data-v")===val);
  });
}
function wire(id, prop, reloadData, gridFilter){
  var box = document.getElementById(id);
  box.addEventListener("click", function(e){
    var b = e.target.closest("button"); if(!b) return;
    setSegOn(id, b.getAttribute("data-v"));
    STATE[prop] = b.getAttribute("data-v");
    // 종류/소스/정렬 필터를 누르면 그리드 뷰로 자동 전환(클러스터 뷰엔 적용 안 되므로)
    if(gridFilter && STATE.view!=="grid"){ STATE.view="grid"; setSegOn("view","grid"); }
    if(reloadData) load(false); else render();
  });
}
wire("view","view",false,false);
wire("ctype","ctype",false,true);
wire("src","src",false,true);
wire("sort","sort",false,true);
wire("time","time",true,false);
document.getElementById("refresh").addEventListener("click", function(){ load(true); });
load(false);
</script>
</body>
</html>
"""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = load_config()
    yt = "켜짐 ✅" if cfg.get("youtube_api_key", "").strip() else "꺼짐 — 키 넣으면 켜짐"
    has_reddit_oauth = cfg.get("reddit_client_id", "").strip() and cfg.get("reddit_client_secret", "").strip()
    rd = "OAuth 전체모드 ✅" if has_reddit_oauth else "무료모드 (밈 + 영상 RSS 순환수집)"
    # Reddit 앱 키가 없을 때만 RSS 백그라운드 순환 수집 시작
    if not has_reddit_oauth:
        threading.Thread(target=rss_refresh_loop, daemon=True).start()
    print("=" * 60)
    print("  🔥 트렌드 보드 (Trend Board)")
    print("=" * 60)
    print(f"  YouTube 자동수집 : {yt}")
    print(f"  Reddit  자동수집 : {rd}")
    print(f"  주소             : http://{HOST if HOST!='0.0.0.0' else '127.0.0.1'}:{PORT}")
    print("=" * 60)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        server.shutdown()


if __name__ == "__main__":
    main()
