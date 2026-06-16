# W3-D2 Submission — <tên của bạn>

> SCAFFOLD — điền sau khi chạy thật trên stack của pack. Không bịa số.

## 3 thứ tôi học được về AIOps pipeline của mình
1. `<ĐIỀN — vd: detector dùng threshold trên mean, nên miss experiment __>`
2. `<ĐIỀN — vd: correlator chỉ temporal, nên ___ ở experiment 4/8>`
3. `<ĐIỀN — vd: RCA rank theo alert count, nên ___ ở experiment 10>`

## 1 fault mà tôi mong pipeline catch nhưng nó miss
- **Experiment:** `<# + tên>`
- **Why I expected detection:** `<hypothesis / tín hiệu lẽ ra phải trip>`
- **Why pipeline missed (hypothesis):** `<noise floor §7.1 / monitoring loop §7.5 / threshold trên mean / ...>`

## 1 trade-off trong design pipeline mà tôi muốn rethink
`<ĐIỀN — vd: temporal correlation rẻ nhưng mù topology; hoặc 3σ-trên-mean giấu
anomaly trên tín hiệu vốn đã nhiễu (Roblox §7.1); hoặc confidence của LLM RCA
không grounded vào evidence §7.4>`

## Scoreboard summary
- detected: `__/10`
- rca_correct: `__/__`
- mttd_p50: `__s`
- false_alarms: `__`
- verdict: `<PASS/FAIL theo §8.6: detected ≥ 7, RCA ≥ 70% của detected, FA ≤ 1>`
