# -*- coding: utf-8 -*-
"""
정적 트렌드 보드 생성기 (GitHub Actions에서 1시간마다 실행).
- trend_server.py의 수집 로직을 재사용해 data.json + index.html 생성.
- API 키는 환경변수(YOUTUBE_API_KEY 등)에서 읽음 → GitHub Secret에 저장.
- 결과 data.json / niche_cache.json 을 커밋하면 GitHub Pages가 서빙.
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import trend_server as ts

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    # 데이터 수집 (force=True로 캐시 무시)
    data = ts.build_trends(force=True)
    data["static"] = True

    with open(os.path.join(HERE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    # 정적 프론트엔드: 서버 API 호출을 정적 파일 로드로 치환
    html = ts.PAGE_HTML.replace(
        'var u = "/api/trends?t="+STATE.time+(force?"&refresh=1":"");',
        'var u = "./data.json";',
    )
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print(f"OK: {data.get('count')} items, {len(data.get('clusters', []))} clusters, "
          f"youtube_ok={data.get('youtube_ok')}, reddit_mode={data.get('reddit_mode')}")


if __name__ == "__main__":
    main()
