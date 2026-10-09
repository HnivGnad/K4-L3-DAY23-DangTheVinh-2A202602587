# Runbook — Region A down, chuyển sang B

Phạm vi: lab local bare-mode, backend filesystem. Chạy tại thư mục gốc dự án trên Linux/WSL, với Python và dependency trong `requirements.txt`. Incident Commander (IC) có quyền quyết định failover và rollback; on-call thực thi. Mục tiêu RTO/RPO: 300s. Không chạy đồng thời hai runbook.

Chuẩn bị trước sự cố: `make seed`, `bash scripts/up_bare.sh`; chạy `python3 state/replicate.py --every 30 --duration 150 --backend fs` và chờ có `state/_replica/dr-artifacts/MANIFEST.json`. Chạy loadgen và health checker ở terminal riêng:

```bash
python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl
```

Hai lệnh trên chạy song song, không chờ loadgen kết thúc mới mở checker. Target B ban đầu sống nhưng chưa ready là bình thường.

| # | Bước | Lệnh thực thi hoặc kiểm tra | Hoàn thành khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status --backend bare`; `tail -n 5 reports/health-events.jsonl` | A có transition UNHEALTHY sau 3 lỗi liên tiếp; B vẫn alive. Runbook sẽ tự probe thêm 3 lần, cách 5s. | On-call |
| 2 | Mở incident và xác nhận | `python3 dr/runbook.py --primary a --target b --backend fs` | Nhập `y` một lần sau khi xác nhận; log `thong_bao_incident` có timestamp operator và outage nếu có chaos log. Lệnh này tự chạy bước 3–7. | IC phê duyệt, on-call chạy |
| 3 | Restore và scale GPU | `cat reports/failover-events.jsonl` | Thứ tự `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`; restore có RPO, docs_lost và embed_model_version. Không gọi failover thêm lần nữa. | DR engineer |
| 4 | Kiểm tra state replica | `curl -fsS http://127.0.0.1:8002/v1/state`; `curl -fsS http://127.0.0.1:8002/readyz` | weights=true, count>0, pool_state=full; readyz HTTP 200 sau warm-up. Log `verify_state_replica` ghi count/weights/version. | AI serving on-call |
| 5 | Kiểm tra cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | Log `5_dns_cutover` sau `4_wait_ready`, edge active_region=b sau TTL tối đa 5s; inference trả region=b. | Network on-call |
| 6 | Golden signals | `cat reports/runbook-run.jsonl`; `curl -fsS http://127.0.0.1:8080/v1/infer` | `verify_golden_signals` có 10 request thật tới B, error_rate=0, p95_latency_ms<1000, ok=true; edge cũng serve B. | AI serving on-call |
| 7 | Đo RTO và postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`; `python3 -m pytest tests/ -v` | valid=true, warnings=[], PASS, recovery từ B; báo cáo ghi RPO giây và docs_lost, timeline path:line, action item có owner/deadline. | IC và DR engineer |

Nếu target không reachable, snapshot không có, hoặc readyz không đạt 200 trong 60s: dừng; runbook trả lỗi, routing giữ nguyên A, không sửa pointer để ép cutover. Nếu cutover xong nhưng golden signals fail: mở escalated incident, giữ bằng chứng; không tự động chuyển qua lại.

Rollback chỉ khi B liên tục lỗi hoặc có lỗi dữ liệu/model đã xác nhận, và A đã được khôi phục, state được đối soát (bao gồm dữ liệu ghi ở B nếu có), có weights/version tương thích và readyz HTTP 200 ít nhất 3 lần cách 5s. IC là người duy nhất cho phép rollback; on-call không tự failback khi A chỉ vừa sống lại.

Trong lab netblock, khôi phục process A bằng `python3 chaos/kill_region.py restore --region a --backend bare`; SIGKILL cần khởi động lại process thay vì SIGCONT. Sau khi IC xác nhận dữ liệu A đúng, chuẩn bị snapshot A bằng `python3 state/snapshot.py put --region a --backend fs`. Kiểm tra A:

```bash
for i in 1 2 3; do curl -fsS http://127.0.0.1:8001/readyz || exit 1; sleep 5; done
```

Nếu cả ba lần thành công và IC phê duyệt, cutover có kiểm soát:

```bash
python3 -c "from pathlib import Path; p=Path('edge/active_region.tmp'); p.write_text('a'); p.replace(Path('edge/active_region'))"
sleep 5
curl -fsS http://127.0.0.1:8080/edge/state
curl -fsS http://127.0.0.1:8080/v1/infer
```

Ghi quyết định rollback vào incident log; xác nhận response region=a. Không dùng `dr/runbook.py --primary b --target a` khi B còn healthy: đó không phải outage và sẽ bị chặn. `--auto` chỉ dành cho drill/CI. Thông báo incident của mã lab là log local; IC phụ trách thông báo cho đội vận hành ngoài lab.
