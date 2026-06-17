# ADR-001: Nạp sự kiện deploy/thay-đổi-cấu-hình làm tín hiệu RCA hạng nhất, xếp trên topology thuần

## Trạng thái

Chấp nhận (2026-06-15). Bổ trợ cho ADR-007 (RCA topology-aware, causal-lag) trong
note W3-D3; không thay thế nó. Biện pháp phòng ngừa "cổng ReDoS pre-deploy" được
theo dõi riêng như một action item P0 của postmortem, không phải ở đây.

## Bối cảnh

Pipeline AIOps (w2/d3 `correlate.py` + `rca.py`) xếp hạng nguyên nhân gốc từ tập
các dịch vụ đang alert dựa vào **khoảng cách topology + severity của alert**: dịch
vụ được-phụ-thuộc-nhiều-nhất trong số đang alert sẽ thắng.

Chạy nó trên bản tái hiện Cloudflare-WAF-regex (`rca_observed.json`) bộc lộ một lỗ
hổng mang tính cấu trúc:

- Pipeline **khoanh đúng thành phần bị nghẽn** — `waf-engine`, confidence 0.515 —
  nhưng **không gọi được tên cái đã làm hỏng nó**. Nguyên nhân gốc thật là *nội dung
  của một artifact cấu hình* (một regex backtracking trong WAF rule `v-vulnerable`).
  Trigger đó là một **sự kiện thay đổi** (`ruleset_deployed`), không phải metric bị
  vượt, nên nó chỉ sống trong `timeline.json` và **không bao giờ** là input của
  pipeline. Người ứng phó tiêu phần lớn MTTR để tự suy lại đúng cái sự thật mà một
  change feed lẽ ra đã trao: "một rule vừa được đẩy".
- Vì rule deploy **nguyên tử và toàn cầu**, mọi dịch vụ vượt ngưỡng trong cùng một
  cửa sổ scrape. Không có thứ tự first-drift / causal-lag nào cho RCA topology-thời-gian
  khai thác ở đây (Lỗ hổng 3 trong `postmortem.md`).
- Truy hồi theo tập-dịch-vụ đặt một playbook **sai cơ chế** lên đầu danh sách action
  khuyến nghị ("Mở rộng bộ nhớ cdn-cache").

Xếp hạng topology trả lời câu *"dịch vụ nào nằm dưới nhiều đau đớn nhất?"* Nó không
trả lời câu *"cái gì đã thay đổi để gây ra đau đớn?"* — đúng cái người ứng phó cần,
và là lớp nguyên-nhân-gốc thống trị trong thực tế (một tỷ lệ lớn sự cố truy về một
lần deploy hoặc một lần đẩy cấu hình).

## Quyết định

Nạp **sự kiện deploy/thay-đổi-cấu-hình** (phiên bản rule, đẩy cấu hình, feature
flag, bản phát hành) từ change/CD bus làm input RCA hạng nhất, song song với metric
alert.

Khi một sự kiện thay đổi (a) **đi trước** lần vượt ngưỡng đầu tiên của một cụm trong
cửa sổ tương quan và (b) **giao với bán kính ảnh hưởng** (dịch vụ đích của nó tới
được theo topology từ các dịch vụ đang alert), thì đưa **artifact thay đổi** (vd.
"WAF rule `v-vulnerable`, đẩy 09:58:36Z") lên làm **ứng viên nguyên-nhân-gốc hàng
đầu**, xếp trên hub topology. Phát ra action khuyến nghị hàng đầu là rollback artifact
đó.

Topology + severity vẫn là bộ xếp hạng khi không có thay đổi tương quan, nên pipeline
suy biến mượt mà về hành vi hiện tại.

## Các phương án đã cân nhắc

- **A. Giữ nguyên — chỉ topology + severity.**
  *Ưu:* đơn giản nhất; không thêm nguồn dữ liệu; hoàn toàn xác định; đã ship; suy
  biến mượt. *Nhược:* đổ trách nhiệm cho hub được-phụ-thuộc-nhiều-nhất (thường *khoẻ
  mạnh*); không gọi được tên artifact; gần như vô dụng với lỗi chỉ-do-cấu-hình nơi
  không dịch vụ nào "hỏng"; tạo ra action mở-rộng-cache gây hiểu lầm trong sự cố này.
  **Bị loại** — nó chính *là* lỗ hổng mà sự cố này bộc lộ.

- **C. Chỉ LLM RCA tự do trên log + diff.**
  *Ưu:* linh hoạt; có thể đọc diff rule và giả thuyết "regex backtracking"; giải
  thích bằng ngôn ngữ tự nhiên. *Nhược:* có thể ảo giác một root tự-tin-nhưng-sai
  (được nêu rõ là anti-pattern trong note W3); thêm độ trễ, chi phí và tính phi-xác-định
  trên đường nóng của sự cố; và nó vẫn cần đúng change/log feed làm input. **Bị loại
  vai trò bộ xếp hạng chính**; giữ làm lớp enrichment best-effort, mặc-định-tắt hiện
  có (`enrich.py`).

- **D. Chỉ causal-lag / first-drift thuần (kiểu ADR-007 trong note W3-D3).**
  *Ưu:* mạnh với cascading failure có gradient lan truyền. *Nhược:* suy biến về
  topology+severity dưới một deploy nguyên tử toàn cầu vì mọi node trôi trong cùng
  một khoảng (quan sát trực tiếp ở đây). **Bị loại vai trò đủ-một-mình** cho lớp lỗi
  này; thứ tự theo thời-điểm-thay-đổi mới là tín hiệu sống sót qua tính đồng thời.

## Hệ quả

- **(+)** Chỉ người ứng phó tới **artifact** gây lỗi và một nút rollback, rút gọn pha
  "cái gì đã thay đổi?" của MTTR — ~3 giây một con người tiêu trong bản tái hiện, và
  hàng phút-tới-giờ nó tốn trong production.
- **(+)** **Mạnh trước deploy nguyên tử toàn cầu** nơi xếp hạng causal-lag suy biến
  (Lỗ hổng 3): timestamp sự kiện thay đổi cung cấp thứ tự mà các metric gần-đồng-thời
  không thể.
- **(−)** Thêm một **phụ thuộc vận hành**: phải nối vào và duy trì một change/deploy
  feed đáng tin, độ-trễ-thấp, *đầy đủ*. Một feed thiếu hoặc trễ sẽ âm thầm kéo RCA về
  chỉ-topology — lỗi này lặng lẽ, nên độ tươi của feed cần giám sát riêng.
- **(−) Rủi ro quy-kết-quá-mức** trong môi trường deploy dày đặc: "có một thay đổi
  gần một sự cố" là chuyện thường, nên tương quan ngây thơ sẽ kích hoạt quá mức.
  *Giảm thiểu:* yêu cầu **cả** đi-trước-về-thời-gian **lẫn** giao bán-kính-ảnh-hưởng
  trước khi đề bạt một thay đổi; thêm chấm điểm rủi ro thay đổi; ship ở chế độ
  **chỉ-gợi-ý** và đo độ chính xác trước khi cho nó vượt topology.
- **(−)** Vô dụng với **thay đổi ngoài-luồng / không-ghi-log** (sửa tay, thay đổi từ
  nhà cung cấp thượng nguồn); những thứ đó vẫn rơi về topology + severity.

## Tham chiếu

Lỗ hổng quan sát được: `rca_observed.json` (root cause `waf-engine`; trigger
`ruleset_deployed` chỉ có trong `timeline.json`, không bao giờ là input của pipeline;
action hàng đầu "Mở rộng bộ nhớ cdn-cache" từ một tiền lệ lệch cơ chế) và
`postmortem.md` lỗ hổng phát hiện #1 và #3.
