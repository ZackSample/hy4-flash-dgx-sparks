# Benchmark: hy4-nothink (hy4-flash)

Started 2026-11-18T04:47:43, finished 2026-11-18T04:48:22, base `http://localhost:8888/v1`, think-off `{"chat_template_kwargs": {"reasoning_effort": "no_think"}}`, think-on `{"chat_template_kwargs": {"reasoning_effort": "no_think"}}`.


## Ability

| category | pass | fail | error | unsupported | no answer |
|---|---|---|---|---|---|
| reasoning | 1/1 | 0 | 0 | 0 | 0 |
| coding | 2/2 | 0 | 0 | 0 | 0 |
| **all** | **3/3** | | | | |

| test | status | expected | got | tokens (reasoning) | time s |
|---|---|---|---|---|---|
| rsn-pipes | pass | 36/7 | 36/7 | 382 (0) | 13.8 |
| code-evaluate | pass | all hidden tests pass | 4/4 tests | 603 (0) | 20.3 |
| code-rate-limiter | pass | all hidden tests pass | 6/6 tests | 135 (0) | 4.7 |
