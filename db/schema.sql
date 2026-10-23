-- =====================================================================
-- My Run Log — Supabase 스키마 (PRD 4장)
-- Supabase 대시보드 > SQL Editor에 통째로 붙여넣고 실행한다.
-- 여러 번 실행해도 안전하도록 작성 (if not exists / or replace).
--
-- 접근 원칙 (PRD 6장, Q1: 공개 URL)
--   * anon(공개 페이지)  : 대시보드 테이블/뷰 SELECT만
--   * sync_log            : RLS on + 정책 없음 → secret key만 접근
--   * 쓰기                : scripts/import_fit.py 등이 secret key로 수행
-- 시간대: 주·일 집계는 Asia/Seoul 기준
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. 테이블
-- ---------------------------------------------------------------------

-- 러닝 활동 (FIT 파일 1개 = 1행, Run/TrailRun/VirtualRun만)
create table if not exists public.activities (
  id                 bigint primary key,                 -- 활동 시작 시각(UTC epoch 초). 같은 파일을 다시 넣어도 같은 행으로 덮어씀
  start_at           timestamptz not null,
  name               text,
  sport_type         text not null,
  distance_m         numeric(9,1) not null check (distance_m >= 0),
  moving_s           integer not null check (moving_s > 0),
  elapsed_s          integer not null,
  elev_gain_m        numeric(7,1),
  avg_hr             numeric(5,1),
  max_hr             numeric(5,1),
  avg_cadence_spm    numeric(5,1),                       -- FIT 케이던스(한 발 기준) × 2
  avg_speed_mps      numeric(6,3),
  gear_id            text,                               -- 수동 지정 (gear.id)
  fit_file           text,                               -- 사용 안 함(항상 null). 파일명에 계정 정보가 있을 수 있어 저장하지 않는다
  polyline           text,                               -- 시작/종료 privacy_radius_m 트리밍 후 인코딩
  decoupling_pct     numeric(5,2),                       -- 60분 이상 활동만
  z1_s integer, z2_s integer, z3_s integer, z4_s integer, z5_s integer,   -- 심박 존별 초
  -- 가민 값 (현재는 수동 입력 또는 비어 있음)
  stride_m_garmin    numeric(4,2),
  gct_ms             numeric(5,1),
  vertical_ratio_pct numeric(4,2),
  vertical_osc_cm    numeric(4,1),
  source_flags       jsonb not null default '{}'::jsonb, -- 예: {"stride":"estimated"}
  synced_at          timestamptz not null default now(),
  -- 파생 지표 (PRD 8장) — 입력 컬럼에서 자동 계산
  pace_s_per_km      numeric(6,1) generated always as
                       (case when distance_m > 0 then moving_s / (distance_m / 1000) end) stored,
  ef                 numeric(5,3) generated always as
                       (case when avg_hr > 0 then (distance_m / moving_s * 60) / avg_hr end) stored,
  stride_m           numeric(4,2) generated always as
                       (coalesce(stride_m_garmin,
                         case when avg_cadence_spm > 0 then (distance_m / moving_s * 60) / avg_cadence_spm end)) stored
);
create index if not exists activities_start_at_idx on public.activities (start_at desc);

-- 활동 스트림 (상세 드로어에서만 조회). 10초 간격 다운샘플링, latlng도 트리밍 후 저장
create table if not exists public.activity_streams (
  activity_id       bigint primary key references public.activities(id) on delete cascade,
  sample_interval_s smallint not null default 10,
  -- {"time":[], "distance":[], "latlng":[[lat,lng]], "altitude":[], "heartrate":[],
  --  "cadence":[], "velocity_smooth":[], "grade_smooth":[]}
  data              jsonb not null,
  updated_at        timestamptz not null default now()
);

-- 구간 최고 기록 (FIT 기록에서 직접 계산)
create table if not exists public.best_efforts (
  activity_id bigint not null references public.activities(id) on delete cascade,
  effort_key  text   not null check (effort_key in ('400m','1k','1mile','5k','10k','half','full')),
  distance_m  numeric(8,1) not null,
  elapsed_s   integer not null check (elapsed_s > 0),
  primary key (activity_id, effort_key)
);
create index if not exists best_efforts_key_idx on public.best_efforts (effort_key, elapsed_s);

-- 신발 (수동 관리)
create table if not exists public.gear (
  id          text primary key,                          -- 예: shoe-a
  name        text not null,
  distance_m  numeric(10,1) not null default 0,          -- 누적 거리 (수동 입력, 없으면 활동 합계로 계산)
  retired     boolean not null default false,
  updated_at  timestamptz not null default now()
);

-- 일별 가민 지표 (수동 입력, PRD 2.1 2단계). 값이 없으면 대시보드가 추정값 사용
create table if not exists public.daily_metrics (
  date            date primary key,
  vo2max          numeric(4,1),
  acute_load      numeric(6,1),
  recovery_hours  numeric(4,1),
  resting_hr      numeric(4,1),
  source          text not null default 'garmin_manual' check (source in ('garmin_manual','garmin_auto')),
  note            text,
  updated_at      timestamptz not null default now()
);

-- 설정 (1행 고정). 기본값 = PRD 11장 Q4 결정
create table if not exists public.settings (
  id               smallint primary key default 1 check (id = 1),
  max_hr           smallint,                             -- null이면 최근 365일 최고 심박으로 자동 추정
  resting_hr       smallint not null default 60,
  target_cadence   smallint not null default 180,
  cadence_band     smallint not null default 5,
  weekly_goal_km   smallint not null default 40,
  hr_zone_pcts     numeric[] not null default '{0.5,0.6,0.7,0.8,0.9}',  -- Z1~Z5 하한 (% of max)
  privacy_radius_m smallint not null default 200,
  updated_at       timestamptz not null default now()
);
insert into public.settings (id) values (1) on conflict (id) do nothing;

-- 동기화 기록 (헤더의 "마지막 동기화" 표시용)
create table if not exists public.sync_log (
  id          bigint generated always as identity primary key,
  source      text not null check (source in ('backfill','manual')),
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  status      text not null default 'running' check (status in ('running','ok','error')),
  activities  integer not null default 0,
  detail      jsonb                                      -- 오류 메시지 등 (anon에 노출 안 함)
);

-- ---------------------------------------------------------------------
-- 2. RLS 및 권한
-- ---------------------------------------------------------------------
alter table public.activities       enable row level security;
alter table public.activity_streams enable row level security;
alter table public.best_efforts     enable row level security;
alter table public.gear             enable row level security;
alter table public.daily_metrics    enable row level security;
alter table public.settings         enable row level security;
alter table public.sync_log         enable row level security;   -- 정책 없음 = anon 접근 불가, 뷰로만 노출

do $$
declare t text;
begin
  foreach t in array array['activities','activity_streams','best_efforts','gear','daily_metrics','settings'] loop
    execute format('drop policy if exists public_read on public.%I', t);
    execute format('create policy public_read on public.%I for select to anon, authenticated using (true)', t);
  end loop;
end $$;

-- 대시보드가 읽는 것만 SELECT 허용, 쓰기 권한은 회수
revoke all on public.activities, public.activity_streams, public.best_efforts, public.gear,
              public.daily_metrics, public.settings, public.sync_log
  from anon, authenticated;
grant select on public.activities, public.activity_streams, public.best_efforts, public.gear,
                public.daily_metrics, public.settings
  to anon, authenticated;

-- ---------------------------------------------------------------------
-- 3. 집계 뷰 (프론트는 뷰만 읽는다). security_invoker → 호출자 권한·RLS 적용
-- ---------------------------------------------------------------------

-- 실제 적용되는 설정값 (최대 심박 자동 추정, 안정 심박은 최근 가민 입력값 우선)
create or replace view public.v_settings with (security_invoker = true) as
select s.target_cadence, s.cadence_band, s.weekly_goal_km, s.hr_zone_pcts, s.privacy_radius_m,
       coalesce(s.max_hr,
                (select max(a.max_hr) from public.activities a where a.start_at >= now() - interval '365 days'))::numeric
         as max_hr,
       coalesce((select d.resting_hr from public.daily_metrics d
                  where d.resting_hr is not null order by d.date desc limit 1),
                s.resting_hr)::numeric
         as resting_hr,
       s.max_hr is null as max_hr_estimated
from public.settings s
where s.id = 1;

-- 활동 + TRIMP (설정이 바뀌어도 항상 현재 기준으로 다시 계산되도록 뷰에서 계산)
create or replace view public.v_activities with (security_invoker = true) as
select a.*,
       (a.start_at at time zone 'Asia/Seoul')::date as local_date,
       case when a.avg_hr is not null and st.max_hr > st.resting_hr then
         a.moving_s / 60.0 * hrr * 0.64 * exp(1.92 * hrr)
       end as trimp
from public.activities a
cross join public.v_settings st
cross join lateral (select greatest(0, least(1, (a.avg_hr - st.resting_hr) / nullif(st.max_hr - st.resting_hr, 0))) as hrr) h;

-- 달력 히트맵·스트릭
create or replace view public.v_calendar with (security_invoker = true) as
select local_date as date,
       round(sum(distance_m) / 1000, 2) as km,
       count(*) as runs
from public.v_activities
group by local_date;

-- 주간 집계 (ISO 주, 월요일 시작)
create or replace view public.v_weekly with (security_invoker = true) as
select date_trunc('week', local_date)::date as week_start,
       round(sum(distance_m) / 1000, 2)                                   as km,
       sum(moving_s)                                                      as moving_s,
       count(*)                                                           as runs,
       round(sum(ef * distance_m) filter (where ef is not null)
             / nullif(sum(distance_m) filter (where ef is not null), 0), 3)        as ef,
       round(sum(avg_cadence_spm * moving_s) filter (where avg_cadence_spm is not null)
             / nullif(sum(moving_s) filter (where avg_cadence_spm is not null), 0), 1) as cadence_spm,
       round(sum(stride_m * moving_s) filter (where stride_m is not null)
             / nullif(sum(moving_s) filter (where stride_m is not null), 0), 3)    as stride_m,
       sum(z1_s) as z1_s, sum(z2_s) as z2_s, sum(z3_s) as z3_s, sum(z4_s) as z4_s, sum(z5_s) as z5_s,
       round(avg(gct_ms), 1)             as gct_ms,
       round(avg(vertical_ratio_pct), 2) as vertical_ratio_pct
from public.v_activities
group by 1;

-- 일별 부하: 급성(7일 합) · 만성(28일 주평균) · ACWR. 활동 없는 날도 0으로 채운다
create or replace view public.v_daily_load with (security_invoker = true) as
with days as (
  select generate_series(
           (select min(local_date) from public.v_activities),
           (now() at time zone 'Asia/Seoul')::date,
           interval '1 day')::date as date
), daily as (
  select d.date, coalesce(sum(a.trimp), 0) as trimp
  from days d
  left join public.v_activities a on a.local_date = d.date
  group by d.date
), win as (
  select date, trimp,
         sum(trimp) over w7  as acute,
         sum(trimp) over w28 / 4 as chronic,
         count(*)  over w28 as days_in_window
  from daily
  window w7  as (order by date rows between 6 preceding and current row),
         w28 as (order by date rows between 27 preceding and current row)
)
select w.date,
       round(w.trimp, 1)   as trimp,
       round(w.acute, 1)   as acute,
       round(w.chronic, 1) as chronic,
       case when w.days_in_window = 28 and w.chronic > 0 then round(w.acute / w.chronic, 2) end as acwr,
       m.acute_load        as garmin_acute_load,
       m.recovery_hours    as garmin_recovery_hours
from win w
left join public.daily_metrics m on m.date = w.date;

-- PR 갱신 이력: 구간별로 그때까지의 최고 기록보다 빠른 기록만
create or replace view public.v_pr_timeline with (security_invoker = true) as
with e as (
  select b.effort_key, b.distance_m, b.elapsed_s, b.activity_id, a.start_at, a.name,
         min(b.elapsed_s) over (partition by b.effort_key order by a.start_at, b.activity_id
                                rows between unbounded preceding and 1 preceding) as prev_best
  from public.best_efforts b
  join public.activities a on a.id = b.activity_id
)
select effort_key, distance_m, elapsed_s, activity_id, start_at, name,
       round(elapsed_s / (distance_m / 1000), 1) as pace_s_per_km
from e
where prev_best is null or elapsed_s < prev_best;

-- VO2max: 가민 수동 입력값이 있으면 그것, 없으면 대시보드가 best effort로 VDOT 계산
create or replace view public.v_vo2max with (security_invoker = true) as
select date, vo2max, 'garmin'::text as source
from public.daily_metrics
where vo2max is not null;

-- 마지막 동기화 (오류 상세는 숨김)
create or replace view public.v_sync_status as
select source, max(finished_at) as last_success_at
from public.sync_log
where status = 'ok'
group by source;
-- v_sync_status는 sync_log RLS(정책 없음)를 우회해야 하므로 security_invoker를 쓰지 않는다.
-- 노출 컬럼은 source·시각뿐이다.

grant select on public.v_settings, public.v_activities, public.v_calendar, public.v_weekly,
                public.v_daily_load, public.v_pr_timeline, public.v_vo2max, public.v_sync_status
  to anon, authenticated;
