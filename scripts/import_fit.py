#!/usr/bin/env python3
"""가민 FIT 파일 → Supabase 가져오기 (M2).

입력은 .fit 파일, .fit이 들어 있는 폴더, 또는 .zip(가민 '데이터 내보내기' 파일 포함, 중첩 zip도 처리)이다.
러닝(sport=running)만 가져오고 나머지 종목은 건너뛴다. 같은 파일을 다시 넣어도
활동 시작 시각을 id로 쓰기 때문에 중복되지 않고 덮어쓴다.

    python3 scripts/import_fit.py ~/Downloads/garmin_export.zip --dry-run   # 요약만 출력
    python3 scripts/import_fit.py ~/Downloads/garmin_export.zip             # Supabase에 저장
    python3 scripts/import_fit.py ~/Downloads/new_run.fit

필요: pip install fitdecode  (Supabase 전송은 표준 라이브러리만 사용)
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
KST = timezone(timedelta(hours=9))
SEMI = 180.0 / 2**31
STREAM_STEP_S = 10
PAUSE_GAP_S = 60                       # 기록 간격이 이보다 크면 일시정지로 보고 시간에서 뺀다
EFFORTS = [("400m", 400.0), ("1k", 1000.0), ("1mile", 1609.344), ("5k", 5000.0),
           ("10k", 10000.0), ("half", 21097.5), ("full", 42195.0)]


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ---------------------------------------------------------------------------
# 입력 수집: 파일/폴더/zip(중첩 포함)에서 (이름, 바이트)를 꺼낸다
# ---------------------------------------------------------------------------
def iter_fit_blobs(path: Path, depth: int = 0) -> Iterator[tuple[str, bytes]]:
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".fit", ".zip"):
                yield from iter_fit_blobs(p, depth)
    elif path.suffix.lower() == ".fit":
        yield path.name, path.read_bytes()
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            yield from _iter_zip(z, path.name, depth)


def _iter_zip(z: zipfile.ZipFile, label: str, depth: int) -> Iterator[tuple[str, bytes]]:
    for info in z.infolist():
        if info.is_dir():
            continue
        low = info.filename.lower()
        if low.endswith(".fit"):
            yield Path(info.filename).name, z.read(info)
        elif low.endswith(".zip") and depth < 3:
            with zipfile.ZipFile(io.BytesIO(z.read(info))) as inner:
                yield from _iter_zip(inner, info.filename, depth + 1)


# ---------------------------------------------------------------------------
# FIT 파싱
# ---------------------------------------------------------------------------
def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_fit(name: str, data: bytes) -> dict | None:
    """러닝 활동이면 {session, records} dict를, 아니면 None을 돌려준다."""
    import fitdecode

    session, records = None, []
    try:
        with fitdecode.FitReader(io.BytesIO(data)) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "session" and session is None:
                    session = {f.name: f.value for f in frame.fields}
                elif frame.name == "record":
                    records.append({f.name: f.value for f in frame.fields})
    except Exception as e:  # 깨진 파일 등
        print(f"  ! {name}: 읽기 실패 ({type(e).__name__}: {e})", file=sys.stderr)
        return None
    if not session or str(session.get("sport")) != "running":
        return None
    return {"session": session, "records": records}


def _g(rec: dict, *keys):
    for k in keys:
        v = _num(rec.get(k))
        if v is not None:
            return v
    return None


def build_series(records: list[dict], start: datetime) -> dict:
    """record 메시지를 이동 시간 기준 시계열로 정리한다. 일시정지 구간은 시간에서 뺀다."""
    t = d = 0.0
    prev_ts = None
    out = {k: [] for k in ("t", "dist", "lat", "lng", "alt", "hr", "cad", "spd")}
    for r in records:
        ts = r.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if prev_ts is not None:
            gap = (ts - prev_ts).total_seconds()
            if gap < 0:
                continue
            t += gap if gap <= PAUSE_GAP_S else 0.0
        prev_ts = ts
        dist = _g(r, "distance")
        if dist is None:
            continue
        d = max(d, dist)
        lat, lng = _num(r.get("position_lat")), _num(r.get("position_long"))
        cad = _g(r, "cadence")
        if cad is not None:
            cad = (cad + (_g(r, "fractional_cadence") or 0.0)) * 2   # 한 발 기준 → spm
        out["t"].append(t)
        out["dist"].append(d)
        out["lat"].append(lat * SEMI if lat is not None else None)
        out["lng"].append(lng * SEMI if lng is not None else None)
        out["alt"].append(_g(r, "enhanced_altitude", "altitude"))
        out["hr"].append(_g(r, "heart_rate"))
        out["cad"].append(cad)
        out["spd"].append(_g(r, "enhanced_speed", "speed"))
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------------------
# 파생 지표
# ---------------------------------------------------------------------------
def encode_polyline(points: list[tuple[float, float]]) -> str:
    """Google Encoded Polyline (precision 5). 프론트의 @mapbox/polyline과 호환."""
    out, plat, plng = [], 0, 0
    for lat, lng in points:
        la, ln = round(lat * 1e5), round(lng * 1e5)
        for v in (la - plat, ln - plng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plng = la, ln
    return "".join(out)


def trimmed_indices(s: dict, radius_m: float) -> list[int]:
    """시작/종료에서 radius_m 이내 구간을 제외한 위치 기록 인덱스."""
    if not s["dist"]:
        return []
    total = s["dist"][-1]
    return [i for i, (dd, la) in enumerate(zip(s["dist"], s["lat"]))
            if la is not None and radius_m <= dd <= total - radius_m]


def best_efforts(s: dict) -> dict[str, float]:
    """이동 시간·누적 거리에서 구간별 최단 시간(초). 구간 끝점은 선형 보간한다."""
    t, dist = s["t"], s["dist"]
    n = len(t)
    res: dict[str, float] = {}
    if n < 2:
        return res
    for key, D in EFFORTS:
        if dist[-1] < D:
            continue
        best, j = math.inf, 0
        for i in range(n):
            target = dist[i] + D
            while j < n and dist[j] < target:
                j += 1
            if j >= n:
                break
            if j == 0 or dist[j] == dist[j - 1]:
                t_end = t[j]
            else:
                f = (target - dist[j - 1]) / (dist[j] - dist[j - 1])
                t_end = t[j - 1] + f * (t[j] - t[j - 1])
            best = min(best, t_end - t[i])
        if math.isfinite(best) and best > 0:
            res[key] = round(best)
    return res


def decoupling_pct(s: dict, moving_s: float) -> float | None:
    """이동 시간 기준 전·후반 EF(속도/심박) 차이 %. 60분 미만이면 None."""
    if moving_s < 3600 or not s["t"]:
        return None
    half_t = s["t"][-1] / 2
    ef = []
    for sel in (lambda i: s["t"][i] <= half_t, lambda i: s["t"][i] > half_t):
        idx = [i for i in range(len(s["t"])) if sel(i)]
        hrs = [s["hr"][i] for i in idx if s["hr"][i]]
        if len(idx) < 2 or not hrs:
            return None
        dt = s["t"][idx[-1]] - s["t"][idx[0]]
        dd = s["dist"][idx[-1]] - s["dist"][idx[0]]
        if dt <= 0:
            return None
        ef.append((dd / dt * 60) / mean(hrs))
    return round((ef[0] - ef[1]) / ef[0] * 100, 2) if ef[0] else None


def zone_seconds(s: dict, max_hr: float, pcts: list[float]) -> list[int]:
    """심박 존 Z1~Z5별 초. 존 하한은 max_hr × pcts, Z1 미만은 Z1에 합산한다."""
    bounds = [p * max_hr for p in pcts[1:]]          # Z2~Z5 하한
    z = [0.0] * 5
    for i in range(1, len(s["t"])):
        hr = s["hr"][i]
        if not hr:
            continue
        k = sum(hr >= b for b in bounds)
        z[k] += s["t"][i] - s["t"][i - 1]
    return [round(x) for x in z]


def downsample_stream(s: dict, step: int = STREAM_STEP_S) -> dict:
    keep, last = [], -step
    for i, t in enumerate(s["t"]):
        if t - last >= step or i == len(s["t"]) - 1:
            keep.append(i)
            last = t
    r1 = lambda x, n=1: None if x is None else round(x, n)
    return {
        "time": [round(s["t"][i]) for i in keep],
        "distance": [round(s["dist"][i], 1) for i in keep],
        "latlng": [[round(s["lat"][i], 6), round(s["lng"][i], 6)] if s["lat"][i] is not None else None for i in keep],
        "altitude": [r1(s["alt"][i]) for i in keep],
        "heartrate": [None if s["hr"][i] is None else round(s["hr"][i]) for i in keep],
        "cadence": [None if s["cad"][i] is None else round(s["cad"][i]) for i in keep],
        "velocity_smooth": [r1(s["spd"][i], 2) for i in keep],
    }


# ---------------------------------------------------------------------------
# 활동 → DB 행
# ---------------------------------------------------------------------------
def build_activity(name: str, parsed: dict, privacy_m: float) -> dict | None:
    ses, recs = parsed["session"], parsed["records"]
    start = ses.get("start_time") or (recs[0].get("timestamp") if recs else None)
    if not isinstance(start, datetime):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    s = build_series(recs, start)
    dist_m = _num(ses.get("total_distance")) or (s["dist"][-1] if s["dist"] else 0.0)
    moving = _num(ses.get("total_timer_time")) or (s["t"][-1] if s["t"] else 0.0)
    if dist_m < 100 or moving < 30:            # 의미 없는 짧은 기록
        return None
    elapsed = _num(ses.get("total_elapsed_time")) or moving

    # 케이던스: session 값(strides/min) → spm. 없으면 record 평균
    cad = _num(ses.get("avg_running_cadence"))
    cad = (cad + (_num(ses.get("avg_fractional_cadence")) or 0.0)) * 2 if cad else mean(s["cad"])

    step_len = _num(ses.get("avg_step_length"))
    if step_len is not None and step_len > 10:  # mm로 들어온 경우
        step_len /= 1000
    stance = _num(ses.get("avg_stance_time"))
    vosc = _num(ses.get("avg_vertical_oscillation"))
    if vosc is not None and vosc > 30:          # mm → cm
        vosc /= 10
    vratio = _num(ses.get("avg_vertical_ratio"))

    idx = trimmed_indices(s, privacy_m)
    poly_pts = [(s["lat"][i], s["lng"][i]) for i in idx][::5]    # 요약 경로는 간격을 성기게
    sub = str(ses.get("sub_sport") or "")
    sport_type = ("TrailRun" if sub == "trail"
                  else "VirtualRun" if sub in ("virtual_activity", "treadmill", "indoor_running") else "Run")
    local = start.astimezone(KST)

    act = {
        "id": int(start.timestamp()),
        "start_at": start.isoformat(),
        "name": f"러닝 {local:%Y-%m-%d %H:%M}",
        "sport_type": sport_type,
        "distance_m": round(dist_m, 1),
        "moving_s": round(moving),
        "elapsed_s": round(elapsed),
        "elev_gain_m": _num(ses.get("total_ascent")),
        "avg_hr": _num(ses.get("avg_heart_rate")) or mean(s["hr"]),
        "max_hr": _num(ses.get("max_heart_rate")) or (max((h for h in s["hr"] if h), default=None)),
        "avg_cadence_spm": round(cad, 1) if cad else None,
        "avg_speed_mps": round(dist_m / moving, 3),
        "polyline": encode_polyline(poly_pts) if len(poly_pts) >= 2 else None,
        "decoupling_pct": decoupling_pct(s, moving),
        "stride_m_garmin": round(step_len, 2) if step_len else None,
        "gct_ms": round(stance, 1) if stance else None,
        "vertical_ratio_pct": round(vratio, 2) if vratio else None,
        "vertical_osc_cm": round(vosc, 1) if vosc else None,
        # 원본 파일명에는 계정 정보가 들어 있을 수 있어 공개 DB에는 저장하지 않는다
        "fit_file": None,
        "source_flags": {"stride": "garmin" if step_len else "estimated", "cadence": "garmin"},
    }
    # 스트림도 같은 트리밍을 적용한 좌표만 저장
    st = downsample_stream(s)
    trim_t = {round(s["t"][i]) for i in idx}
    st["latlng"] = [ll if (ll is not None and t in trim_t) else None for ll, t in zip(st["latlng"], st["time"])]
    # 실내(트레드밀) 거리는 시계 추정치라 개인 최고 기록에는 넣지 않는다
    efforts = best_efforts(s) if sport_type != "VirtualRun" else {}
    return {"activity": act, "stream": st, "efforts": efforts, "series": s}


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
class Supabase:
    def __init__(self, url: str, key: str):
        self.url, self.h = url.rstrip("/"), {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):
            self.h["Authorization"] = f"Bearer {key}"

    def _req(self, path: str, *, method="GET", body=None, prefer: str | None = None):
        h = dict(self.h)
        if prefer:
            h["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Supabase {e.code} {path}\n{e.read().decode(errors='replace')[:600]}") from None

    def upsert(self, table: str, rows: list[dict], on_conflict: str, chunk: int = 100):
        for i in range(0, len(rows), chunk):
            self._req(f"{table}?on_conflict={on_conflict}", method="POST", body=rows[i:i + chunk],
                      prefer="resolution=merge-duplicates,return=minimal")

    def insert(self, table: str, row: dict):
        return self._req(table, method="POST", body=row, prefer="return=representation")

    def patch(self, table: str, flt: str, row: dict):
        self._req(f"{table}?{flt}", method="PATCH", body=row, prefer="return=minimal")

    def get(self, path: str):
        return self._req(path)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="가민 FIT → Supabase")
    ap.add_argument("path", type=Path, help=".fit / 폴더 / .zip")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 요약만 출력")
    ap.add_argument("--privacy-radius", type=float, default=None, help="경로 트리밍 반경(m). 기본: settings 값, 없으면 200")
    ap.add_argument("--max-hr", type=float, default=None, help="존 계산용 최대 심박 (기본: settings → 가장 최근 활동 기준 1년 최고값)")
    ap.add_argument("--min-km", type=float, default=1.0, help="이보다 짧은 기록은 건너뜀 (기본 1.0)")
    ap.add_argument("--pace-range", type=float, nargs=2, default=(150, 900), metavar=("FAST", "SLOW"),
                    help="평균 페이스(초/km)가 이 범위를 벗어나면 측정 오류로 보고 건너뜀 (기본 150 900)")
    args = ap.parse_args()

    if not args.path.exists():
        sys.exit(f"경로가 없습니다: {args.path}")
    try:
        import fitdecode  # noqa: F401
    except ImportError:
        sys.exit("fitdecode가 필요합니다:  pip install fitdecode")

    env = load_env()
    sb = None
    if not args.dry_run:
        if not env.get("SUPABASE_URL") or not env.get("SUPABASE_SECRET_KEY"):
            sys.exit(f"{ENV_PATH} 에 SUPABASE_URL / SUPABASE_SECRET_KEY 가 필요합니다 (또는 --dry-run).")
        sb = Supabase(env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"])

    privacy = args.privacy_radius
    pcts = [0.5, 0.6, 0.7, 0.8, 0.9]
    cfg_max_hr = args.max_hr
    if sb:
        st = sb.get("v_settings?select=privacy_radius_m,hr_zone_pcts,max_hr")
        if st:
            privacy = privacy if privacy is not None else float(st[0]["privacy_radius_m"])
            pcts = [float(x) for x in st[0]["hr_zone_pcts"]]
            cfg_max_hr = cfg_max_hr or (float(st[0]["max_hr"]) if st[0]["max_hr"] else None)
    privacy = 200.0 if privacy is None else privacy

    total = skipped = failed = 0
    built = []
    skip_reasons: dict[str, int] = {}
    seen: set[int] = set()
    for name, blob in iter_fit_blobs(args.path):
        total += 1
        parsed = parse_fit(name, blob)
        if parsed is None:
            skipped += 1
            continue
        item = build_activity(name, parsed, privacy)
        if item is None:
            skipped += 1
            continue
        a = item["activity"]
        pace = a["moving_s"] / (a["distance_m"] / 1000)
        reason = ("짧은 기록" if a["distance_m"] < args.min_km * 1000
                  else "페이스 이상치" if not (args.pace_range[0] <= pace <= args.pace_range[1]) else None)
        if reason:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            skipped += 1
            continue
        if item["activity"]["id"] in seen:
            skipped += 1
            continue
        seen.add(item["activity"]["id"])
        built.append(item)
    print(f"FIT {total}개 읽음 → 러닝 {len(built)}개, 건너뜀 {skipped}개"
          + (f" (이 중 {', '.join(f'{k} {v}' for k, v in skip_reasons.items())})" if skip_reasons else ""))
    if not built:
        return

    if cfg_max_hr:
        max_hr = cfg_max_hr
    else:   # DB의 v_settings와 같은 규칙: 가장 최근 활동 기준 1년 최고 심박
        newest = max(i["activity"]["start_at"] for i in built)
        cutoff = (datetime.fromisoformat(newest) - timedelta(days=365)).isoformat()
        recent = [i["activity"]["max_hr"] or 0 for i in built if i["activity"]["start_at"] >= cutoff]
        max_hr = max(recent or [0]) or max((i["activity"]["max_hr"] or 0) for i in built) or 190.0
    for it in built:
        z = zone_seconds(it["series"], max_hr, pcts)
        it["activity"].update({f"z{k + 1}_s": v for k, v in enumerate(z)})

    built.sort(key=lambda i: i["activity"]["start_at"])
    for it in built[-5:]:
        a = it["activity"]
        pace = a["moving_s"] / (a["distance_m"] / 1000)
        print(f"  · {a['start_at'][:10]}  {a['distance_m'] / 1000:5.2f}km  {int(pace // 60)}:{int(pace % 60):02d}/km  "
              f"심박 {a['avg_hr'] and round(a['avg_hr'])}  케이던스 {a['avg_cadence_spm']}  "
              f"경로 {'O' if a['polyline'] else 'X'}  PR후보 {','.join(it['efforts']) or '-'}")
    print(f"존 계산 기준 최대 심박 {max_hr:.0f}bpm, 경로 트리밍 {privacy:.0f}m")

    if args.dry_run:
        print("(dry-run: 저장하지 않았습니다)")
        return

    log = sb.insert("sync_log", {"source": "manual"})
    log_id = log[0]["id"]
    try:
        sb.upsert("activities", [i["activity"] for i in built], "id")
        sb.upsert("activity_streams", [{"activity_id": i["activity"]["id"], "sample_interval_s": STREAM_STEP_S,
                                        "data": i["stream"]} for i in built], "activity_id", chunk=20)
        efforts = [{"activity_id": i["activity"]["id"], "effort_key": k, "distance_m": dict(EFFORTS)[k], "elapsed_s": v}
                   for i in built for k, v in i["efforts"].items()]
        if efforts:
            sb.upsert("best_efforts", efforts, "activity_id,effort_key")
        sb.patch("sync_log", f"id=eq.{log_id}", {"status": "ok", "finished_at": datetime.now(timezone.utc).isoformat(),
                                                  "activities": len(built)})
    except BaseException as e:
        sb.patch("sync_log", f"id=eq.{log_id}", {"status": "error", "finished_at": datetime.now(timezone.utc).isoformat(),
                                                  "detail": {"error": str(e)[:500]}})
        raise
    print(f"저장 완료: 활동 {len(built)}건, 구간 기록 {len(efforts)}건")


if __name__ == "__main__":
    main()
