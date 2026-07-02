# 트렌드 보드 (정적 · 자동갱신)

전세계 인기 영상·밈·해외광고 트렌드를 모아 보는 정적 사이트. **GitHub Actions가 1시간마다** 데이터를 갱신하고 **GitHub Pages**가 서빙 → 링크만 있으면 누구나 봄.

## 구조
- `generate.py` — 수집 로직 실행 → `data.json` + `index.html` 생성 (trend_server.py 재사용)
- `index.html` + `data.json` — GitHub Pages가 서빙하는 정적 결과물
- `.github/workflows/update.yml` — 매시 정각 자동 실행(수동 실행도 가능)
- `niche_cache.json` — YouTube 니치 검색 캐시(6h). 커밋해서 쿼터 절약.

## 배포 (1회 세팅)
1. GitHub 새 repo 생성 → 이 폴더 push
2. **Settings → Secrets and variables → Actions → New secret**
   - `YOUTUBE_API_KEY` = 유튜브 API 키 (필수)
   - (선택) `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `ANTHROPIC_API_KEY`
3. **Settings → Pages** → Source: `main` 브랜치 `/ (root)`
4. **Actions 탭 → update-trends → Run workflow** 로 첫 갱신 실행
5. `https://<계정>.github.io/<repo>/` 접속

## 주의
- API 키는 **절대 커밋 금지** — GitHub Secret에만.
- 클라우드(Actions) IP에선 Reddit 직접 RSS가 막힐 수 있음. YouTube·니치·해외광고(TVCF)·meme-api는 정상.
