# M1 설정 가이드 — Garmin 데이터 내보내기 · Supabase

계정 생성과 키 발급은 본인이 직접 해야 하는 단계입니다. 전부 무료입니다. 예상 소요 시간은 20분입니다.

## 0. 가민 전체 데이터 내보내기 신청 (먼저, 2분)

준비되는 데 시간이 걸리므로 가장 먼저 신청합니다.

1. PC에서 <https://www.garmin.com/account/datamanagement/exportdata/> 접속 후 로그인
   - 안 열리면: garmin.com 로그인 → 계정 → **데이터 관리** → **데이터 내보내기**
2. **내보내기 요청**을 누릅니다.
3. 준비되면 가민 계정 이메일로 다운로드 링크가 옵니다. zip 파일을 받아 둡니다.

## 1. Supabase 프로젝트 + 스키마

1. <https://supabase.com/dashboard> → New project
   - Name: `run-dashboard`
   - Region: **Northeast Asia (Seoul)**
   - Database Password: 자동 생성 후 비밀번호 관리자에 저장
2. 준비되면(1~2분) **SQL Editor** → New query
3. [`db/schema.sql`](../db/schema.sql) 전체를 붙여넣고 **Run** → "Success. No rows returned"
   - 여러 번 실행해도 안전합니다 (스키마 수정 시 다시 실행)
4. **Table Editor**에 테이블 7개가 생겼는지 확인
   - `activities`, `activity_streams`, `best_efforts`, `gear`, `daily_metrics`, `settings`, `sync_log`
5. **Project Settings → API Keys**에서 확인
   - **Project URL**
   - **Publishable key** (`sb_publishable_…`): 대시보드(프론트)용 공개 키
   - **Secret key** (`sb_secret_…`): 스크립트 전용. 절대 공개 금지

## 2. 로컬 준비

```bash
cd running-dashboard
cp .env.example .env
python3 -m pip install -r scripts/requirements.txt
```

`.env`를 열어 `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY`를 채웁니다. `.env`는 git에 올라가지 않습니다. 키 값은 채팅에 붙여넣지 마세요.

## 3. 가져오기 시험 (내보내기 메일이 오기 전에도 가능)

Garmin Connect 웹(<https://connect.garmin.com>)에서 최근 러닝 하나를 열고 **⚙ → 원본 내보내기(Export Original)**로 FIT 파일(zip 안에 들어 있음)을 받습니다.

```bash
python3 scripts/import_fit.py ~/Downloads/내려받은파일.zip --dry-run
```

러닝 개수와 거리·페이스·심박·케이던스, 경로 유무가 출력되면 정상입니다. 그다음 `--dry-run`을 빼고 실행하면 Supabase에 저장됩니다.

## 4. 과거 기록 전체 가져오기 (M2, 내보내기 메일이 오면)

받은 zip을 그대로 넣으면 됩니다. 안의 zip과 폴더까지 알아서 풀고, 러닝이 아닌 활동은 건너뜁니다.

```bash
python3 scripts/import_fit.py ~/Downloads/가민내보내기.zip --dry-run
python3 scripts/import_fit.py ~/Downloads/가민내보내기.zip
```

## 5. 평소 갱신

가끔(예: 주 1회) 최근 활동의 FIT를 받아 같은 명령을 실행합니다. 같은 활동을 다시 넣어도 덮어쓰기라 중복되지 않습니다.

## 완료 체크리스트

- [ ] 가민 데이터 내보내기 신청함
- [ ] Supabase 테이블 7개 생성, `settings`에 기본 1행 존재
- [ ] `.env` 작성
- [ ] FIT 하나로 `--dry-run` 성공
- [ ] Supabase `activities`에 행이 들어옴

## 문제 해결

| 증상 | 원인 / 해결 |
|------|-------------|
| `fitdecode가 필요합니다` | `python3 -m pip install -r scripts/requirements.txt` |
| 러닝 0개로 나옴 | 러닝이 아닌 활동이거나, 100m 미만의 기록은 건너뜁니다 |
| `Supabase 401` | `SUPABASE_SECRET_KEY`에 publishable key를 넣지 않았는지 확인 |
| `Supabase 404 ... relation` | 스키마(1단계 3번)를 아직 실행하지 않음 |
