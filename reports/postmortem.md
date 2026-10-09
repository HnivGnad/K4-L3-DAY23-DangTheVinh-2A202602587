# Postmortem — DR Drill Lab 23

Ngày 2026-10-09. Mô phỏng local netblock Region A bằng SIGSTOP; blameless: thiếu cơ chế phục hồi trong baseline là vấn đề thiết kế hệ thống.

## 1. Timeline (UTC)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T02:28:27.777+00:00 | Outage A; B vẫn alive, không forced double outage | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T02:28:29.813+00:00 | Request đầu tiên bị lỗi | `reports/drill-2-withdr.jsonl:27` |
| 2026-10-09T02:28:42.032+00:00 | Health checker xác nhận 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T02:28:45.352+00:00 | Runbook xác nhận và mở incident; --auto trong drill | `reports/runbook-run.jsonl:2` |
| 2026-10-09T02:28:45.495+00:00 | Restore vector DB và weights/version hoàn tất | `reports/failover-events.jsonl:2` |
| 2026-10-09T02:28:51.920+00:00 | B sẵn sàng sau warm-up | `reports/failover-events.jsonl:4` |
| 2026-10-09T02:28:51.925+00:00 | Cutover pointer sang B | `reports/failover-events.jsonl:5` |
| 2026-10-09T02:28:56.422+00:00 | Request thành công đầu tiên từ B | `reports/drill-2-withdr.jsonl:40` |

Timestamp loadgen là lúc bắt đầu request, đúng quy ước tools/measure_rto.py. Thời gian hoàn tất request bằng timestamp cộng latency_ms/1000. Operator delay = 17.576s; mode auto chỉ dành cho diễn tập, không giả định người vận hành thật phản hồi nhanh tương tự.

## 2. RTO/RPO và gap analysis

- RTO mục tiêu 300s; thực tế 28.6s; gap = thực tế − mục tiêu = -271.40s (đạt, dư 271.40s).
- RPO mục tiêu 300s; thực tế 4.0s / 2 docs; gap = -296.00s (đạt theo mục tiêu lab).
- Baseline: 15 request fail, NO_RECOVERY trong 40s. Sau DR: 13 request fail rồi B phục hồi, valid=true, warnings=[]. Evidence: `reports/measure-drill-1.json`, `reports/measure-drill-2.json`.
- Budget phát hiện 15s là thành phần lớn; phát hiện thực tế 14.255s. Warm-up 6.4084s; TTL/chờ request 4.4969s. Snapshot restore chỉ 0.0768s trên dữ liệu giả nhỏ, không suy diễn cho model nhiều GB hoặc vector index lớn.
- Golden signals: 10 request trực tiếp B, p95 21.8ms, lỗi 0.0; `reports/runbook-run.jsonl:6`. Đây không phải benchmark tải cao hoặc kiểm chứng chất lượng model thật.

## 3. Root cause — 5 whys

1. Vì sao user lỗi? Edge còn gửi đến A đang bị pause, upstream timeout.
2. Vì sao edge không đổi tuyến? Baseline chỉ đọc pointer với TTL, không có health checker/runbook.
3. Vì sao không thể đổi ngay sang B? B sống nhưng thiếu vectors, weights và pool full; liveness không bảo đảm inference.
4. Vì sao state có thể mất? Snapshot định kỳ 30s chưa chứa ingest mới; compute, routing và state có lịch phục hồi riêng.
5. Vì sao sự cố thật vẫn có thể thất bại? Nếu snapshot thiếu/hỏng, version không tương thích, B unavailable, hoặc warm-up vượt timeout, restore/cutover phải abort. Bài lab kiểm soát môi trường; cần backup nhất quán, kiểm tra integrity/version và diễn tập định kỳ để phát hiện các gap đó trước outage.

Đã xử lý: detector chạy ngoài serving, anti-flap theo region, restore trước scale, ready trước DNS, abort giữ pointer, xác nhận y/N mặc định, một failover duy nhất. Giới hạn: chưa kiểm thử MinIO/cloud, chưa có distributed lock giữa nhiều operator, chưa có data merge khi failback và thông báo incident chỉ là log local.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Diễn tập interval=1s, giữ threshold=3 và confirm; so sánh false alarm | SRE | 2026-10-16 | Budget detection 15→3s, giảm danh nghĩa 12s; phải đo lại RTO |
| 2 | Diễn tập replication=5s và kiểm tra SQLite backup nhất quán | Data engineer | 2026-10-16 | Budget lag snapshot 30→5s; chi phí I/O tăng; không bảo đảm docs_lost=0 |
| 3 | Kiểm thử snapshot checksum, version compatibility và file lỗi/thiếu | ML platform engineer | 2026-10-23 | Giảm rủi ro restore sai; chưa có số giây đo được |
| 4 | Thêm lock/circuit breaker liên operator và phê duyệt failback có đối soát dữ liệu | Incident Commander + SRE | 2026-10-23 | Tránh failover chồng/flapping; chưa đo tác động RTO |
| 5 | Chạy game day trên dữ liệu/model gần production, lưu evidence và alert ngoài process | SRE | 2026-10-30 | Xác nhận mục tiêu 300s ngoài mock; chưa đo |

## 5. Câu hỏi bắt buộc và reflection

1. interval × threshold = 5 × 3 = 15s, chiếm 52.45% RTO đo được. Đây là budget quy ước lab; với poll tức thì, detection thực tế còn phụ thuộc pha poll, khoảng threshold−1 và timeout. Muốn RTO 300s, interval phải nằm trong (300 − restore − warm-up − TTL − overhead)/threshold; không dùng 100s vì không còn budget cho các bước khác.
2. Hạ interval xuống 1s làm budget danh nghĩa giảm 12s. Detector chạy 5 lần thường xuyên hơn; outage ngắn có thể đủ 3 lỗi trong 2 khoảng thay vì 10s, tăng false positives. Giữ confirm/circuit breaker, đo lại thay vì khẳng định chắc RTO giảm đúng 12s.
3. Nếu A mất dữ liệu vĩnh viễn trong 6h, 2 docs là dữ liệu primary có mà snapshot đã restore không có tại thời điểm đo. Khách hàng có thể mất ticket mới hoặc retrieval không phản ánh cập nhật. Không có nghĩa mất toàn bộ 6h dữ liệu; số mất thật cần kiểm tra lịch ingest/replication và bản backup còn dùng được. Ingest trong lab chạy độc lập với process A bị pause.
4. Muốn giảm thời gian mà ít tăng rủi ro flapping: giữ B ở full giảm warm-up khoảng 6.41s nhưng tăng chi phí compute; giảm TTL tác động routing mà không đổi ngưỡng outage.
5. Health checker không import serving và chạy process riêng, nên A chết không kéo detector chết theo. Tuy nhiên WSL/host chết thì toàn bộ lab mất; chưa chứng minh độc lập failure domain thật.
6. Khi cần chứng minh RTO 5 phút: mở `reports/measure-drill-2.json`, rồi đối chiếu `chaos/chaos-events.jsonl:3` và `reports/drill-2-withdr.jsonl:40`; dùng phân rã trong `reports/rto-evidence.md`.

Screenshot được để lại làm sau theo yêu cầu; evidence hiện dùng JSONL và timestamp thực tế.
