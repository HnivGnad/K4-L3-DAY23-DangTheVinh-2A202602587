"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append a timestamped event and expose it to the operator."""
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm, verify, then atomically publish the target pointer."""
    if target not in URL or backend not in {"fs", "minio"} or wait <= 0:
        raise ValueError("invalid failover target, backend, or wait")
    active = pathlib.Path("edge/active_region")
    current = active.read_text().strip() if active.exists() else "a"
    if current == target:
        return {"ok": False, "error": "target_already_active", "target": target}
    stage = "1_verify_target"
    try:
        before = state_of(target)
        emit(step=stage, target=target, state=before)
        stage = "2_restore_snapshot"
        started = time.monotonic()
        meta = snapshot.get(target, backend)
        source = meta.get("source_region", "a" if target == "b" else "b")
        if source == target:
            raise ValueError("snapshot source must differ from target")
        loss = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                            pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        emit(step=stage, target=target, **loss,
             embed_model_version=meta["embed_model_version"],
             snapshot_at=meta["snapshot_at"], restore_s=round(time.monotonic() - started, 4))
        stage = "3_scale_pool"
        pool = pathlib.Path(f"state/region-{target}/pool_state")
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text("full", encoding="utf-8")
        emit(step=stage, target=target, pool_state="full")
        stage = "4_wait_ready"
        started = time.monotonic()
        deadline = started + wait
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2, max(0.001, deadline - time.monotonic())))
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(max(0, min(0.25, deadline - time.monotonic())))
        else:
            raise TimeoutError("target readiness timed out; routing unchanged")
        after = state_of(target)
        emit(step=stage, target=target, waited_s=round(time.monotonic() - started, 4), state=after)
        stage = "5_dns_cutover"
        pending = active.with_suffix(".tmp")
        pending.write_text(target, encoding="utf-8")
        pending.replace(active)
        emit(step=stage, target=target, previous_region=current, ok=True)
        return {"ok": True, "target": target, "state": after, "replica": meta, **loss}
    except (Exception, SystemExit) as exc:
        emit(event="abort", failed_step=stage, target=target, error=str(exc))
        return {"ok": False, "target": target, "failed_step": stage, "error": str(exc)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)
