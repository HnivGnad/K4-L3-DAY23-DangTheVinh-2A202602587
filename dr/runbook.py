"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Record an operator checklist milestone."""
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Require explicit operator confirmation outside the lab/CI."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Confirm a sustained outage and execute one failover transaction."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different valid regions")
    started = time.monotonic()
    failures = 0
    for attempt in range(3):
        ready, reason = hc.probe(primary, 2)
        try:
            other = httpx.get(f"{URL[target]}/healthz", timeout=2)
            other.raise_for_status()
        except httpx.HTTPError as exc:
            step(1, "xac_nhan_outage", ok=False, error=f"target unavailable: {exc}")
            return {"ok": False, "error": "target_unavailable"}
        failures = 0 if ready else failures + 1
        if ready:
            step(1, "xac_nhan_outage", ok=False, reason="primary_ready")
            return {"ok": False, "error": "outage_not_confirmed"}
        if attempt < 2:
            time.sleep(5)
    step(1, "xac_nhan_outage", ok=True, primary=primary, target=target,
         consecutive_fails=failures, reason=reason)
    if not confirm(auto, f"Fail over {primary} -> {target}?"):
        return {"ok": False, "error": "operator_declined"}
    kills = []
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_log.exists():
        kills = [json.loads(line) for line in chaos_log.read_text().splitlines() if line.strip()]
        kills = [e for e in kills if e.get("action") == "kill" and e.get("region") == primary]
    outage = kills[-1]["ts"] if kills else None
    announced = step(2, "thong_bao_incident", primary=primary, target=target,
                     t_outage=outage, notification_delay_s=None if outage is None else time.time() - outage,
                     auto=auto)
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", result=result, ok=result.get("ok", False))
    if not result.get("ok"):
        return result
    state = result["state"]
    step(4, "verify_state_replica", count=state.get("count"), weights=state.get("weights"),
         embed_model_version=result["replica"]["embed_model_version"])
    step(5, "dns_cutover", target=target, ok=result["ok"])
    latencies, errors = [], 0
    with httpx.Client(timeout=3) as client:
        for i in range(10):
            t = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer", params={"q": f"golden signal {i}"})
                errors += int(response.status_code != 200 or response.json().get("region") != target)
            except (httpx.HTTPError, ValueError):
                errors += 1
            latencies.append((time.monotonic() - t) * 1000)
    # Nearest-rank p95 of ten samples is the maximum, not interpolated p90.
    p95 = sorted(latencies)[-1]
    golden_ok = errors == 0 and p95 < 1000
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=round(p95, 2),
         error_rate=errors / 10, ok=golden_ok)
    step(7, "post_incident", elapsed_s=round(time.monotonic() - started, 2),
         t_operator=announced["ts"], t_outage=outage, ok=golden_ok,
         measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {**result, "ok": golden_ok, "cutover_ok": True,
            "golden_signals": {"p95_latency_ms": round(p95, 2), "error_rate": errors / 10}}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)
