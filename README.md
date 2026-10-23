# My Run Log

가민 러닝 기록을 시계열로 분석하는 개인 대시보드입니다. 정적 HTML(`index.html`) 한 장이 Supabase에서 데이터를 읽어 그리고, GitHub Pages로 배포합니다.

**https://scv0413.github.io/run-dashboard/**

- 데이터: 가민 Connect에서 내보낸 FIT 파일 → `scripts/import_fit.py` → Supabase
- 기록이 없거나 연결이 안 되면 샘플 데이터로 보입니다 (`?mock=1`로 강제 가능)
- 기획: [PRD.md](PRD.md) · 설정 방법: [docs/M1_setup.md](docs/M1_setup.md) · 스키마: [db/schema.sql](db/schema.sql)

## 갱신 방법

```bash
python3 -m pip install -r scripts/requirements.txt
python3 scripts/import_fit.py ~/Downloads/가민파일.zip --dry-run   # 미리보기
python3 scripts/import_fit.py ~/Downloads/가민파일.zip             # 저장
```

`.env`(Supabase URL·secret key)는 저장소에 올라가지 않습니다. 대시보드에는 읽기 전용 publishable key만 들어 있습니다.
