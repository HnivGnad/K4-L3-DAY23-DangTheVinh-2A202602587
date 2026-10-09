# RTO/RPO Evidence — Lab 23

Ngày thực hiện: 2026-10-09. Hai region và proxy chạy bằng uvicorn bare-mode trong WSL docker-desktop, không chạy container; Python 3.12.14, backend fs, netblock --mock (SIGSTOP). Warm-up 6s, TTL 5s, checker interval 5s/threshold 3, timeout 2s. Snapshot lặp mỗi 30s; ingest 0.5 document/s. Chỉ gọi localhost. Log giữ timestamp thực tế; không chỉnh timestamp.

## 1. Baseline không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| Outage bắt đầu | 2026-10-09T02:27:35 UTC | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên | +1.996s | request bắt đầu sau kill, ok=false | `reports/drill-1-nodr.jsonl:18` |
| Request thất bại sau kill | 15 | count từ measure | `reports/measure-drill-1.json:28` |
| Recovery | Không có trong cửa sổ 40s | không có request thành công sau lỗi | `reports/measure-drill-1.json:23` |
| RTO | NO_RECOVERY | tools/measure_rto.py | `reports/measure-drill-1.json:25` |

Trước attack, A có 200 docs/weights/full; B có 0 docs, weights=false, pool warm: `reports/baseline-state.json`. Không có detector hay cutover ở baseline. Nếu đổi tuyến sang B ngay, inference trả region_not_ready. NO_RECOVERY chỉ kết luận trong cửa sổ đo, không suy diễn thời gian vô hạn.

## 2. Có DR

| Mốc | Giây từ outage | Cách đo | Evidence |
|---|---|---|---|
| Outage | 0s; 2026-10-09T02:28:27 UTC | action=kill | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +2.037s | request ok=false | `reports/drill-2-withdr.jsonl:27` |
| Health checker phát hiện | +14.255s | A chuyển UNHEALTHY | `reports/health-events.jsonl:2` |
| Operator mở incident | +17.576s | thong_bao_incident | `reports/runbook-run.jsonl:2` |
| Restore hoàn thành | +17.718s | 2_restore_snapshot | `reports/failover-events.jsonl:2` |
| B ready | +24.143s | 4_wait_ready | `reports/failover-events.jsonl:4` |
| DNS cutover | +24.148s | 5_dns_cutover | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **28.6s** | request đầu tiên thành công từ b sau lỗi | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO inference | 28.6s | 300s | PASS | `reports/measure-drill-2.json:20` |
| RPO vector DB tại restore | 4.0s / 2 docs | 300s | PASS | `reports/failover-events.jsonl:2` |
| Embedding model version | embed-model=vi-e5-base@v3 | version đi cùng weights/index | Đã restore | `reports/failover-events.jsonl:2` |
| Golden signals trực tiếp B | p95 21.8ms / error rate 0.0 | p95<1000ms / lỗi=0 | PASS | `reports/runbook-run.jsonl:6` |

Drill valid=True, warnings=[]; failed requests=13. RPO được tính bằng max ingest timestamp primary trừ bản restore, docs_lost đếm docs có timestamp mới hơn bản restore. Ingest chạy độc lập với serving nên số này là RPO **tại restore**, không phải chỉ dữ liệu đã tồn tại lúc outage. Replication có thể tiếp tục sau cutover; đối chiếu dùng RPO đã ghi trong log restore, không lấy snapshot cuối cùng.

## 3. Phân rã RTO

| Thành phần | Giây | Evidence và ý nghĩa | Cách giảm |
|---|---|---|---|
| Health-check detect floor theo quy ước lab | 15.0000s | interval_s × threshold; `reports/health-events.jsonl:2` | Giảm interval, giữ threshold và xác nhận operator |
| Snapshot restore | 0.0768s | duration thao tác get + rpo; `reports/failover-events.jsonl:2` | Snapshot nhất quán và storage nhanh |
| GPU pool warm-up và poll readiness | 6.4084s | waited_s gồm probe state sau ready; `reports/failover-events.jsonl:4` | Pool full dự phòng, tăng chi phí compute |
| DNS/LB TTL và chờ request kế tiếp | 4.4969s | recovered.ts − cutover.ts; `reports/drill-2-withdr.jsonl:40`, `reports/failover-events.jsonl:5` | TTL nhỏ hơn, tăng DNS lookup |
| Xác nhận incident, HTTP verify và scheduling còn lại | 2.6179s | RTO trừ bốn thành phần trên; `reports/runbook-run.jsonl:2`, `reports/measure-drill-2.json:20` | Tránh probe lặp sau detector đã xác nhận |
| **Tổng** | **28.6000s** | Bằng RTO đo được đã làm tròn 0.1s | |

15s là budget interval × threshold mà bài lab yêu cầu ghi, không phải lower bound vật lý cho mọi pha poll: 3 probe lỗi trải trên 2 khoảng interval; thời điểm outage trong chu kỳ và timeout ảnh hưởng detection thực tế. Lượt này detection thực tế là 14.255s. Cột residual hấp thụ phần chênh giữa budget này và detection thực tế, các thao tác verify/confirm, scheduling và sai số làm tròn. Không cộng các mốc tích lũy để tránh đếm thời gian hai lần.
