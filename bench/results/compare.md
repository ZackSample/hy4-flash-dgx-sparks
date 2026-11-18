# hy4 vs glm vs haiku

- **hy4**: model `hy4-flash`, started 2026-11-18T03:49:07, think-off `{"chat_template_kwargs": {"reasoning_effort": "no_think"}}`, think-on `{"chat_template_kwargs": {"reasoning_effort": "high"}}`
- **glm**: model `GLM-5.3-Flash-EXL3`, started 2026-11-18T01:15:48, think-off `{"chat_template_kwargs": {"enable_thinking": false}}`, think-on `{"reasoning_effort": "high"}`
- **haiku**: model `haiku`, offline answers from `bench/results/offline`, graded 2026-11-18T00:59:02

## Speed

| metric | hy4 | glm | haiku |
|---|---|---|---|
| decode-prose: median tok/s | 14.8 | 51.7 | - |
| decode-prose: median TTFT | 0.452 s | 0.121 s | - |
| decode-prose: median tokens | 1000 | 1000 | - |
| decode-code: median tok/s | 29.7 | 72.3 | - |
| decode-code: median TTFT | 0.693 s | 0.210 s | - |
| decode-code: median tokens | 1500 | 1500 | - |
| decode-thinking: tok/s | 23.1 | 48.9 | - |
| decode-thinking: reasoning tokens | 1318 | 2476 | - |
| decode-thinking: answer tokens | 404 | 221 | - |
| decode-thinking: total time | 76.3 s | 55.5 s | - |
| decode-thinking: correct | yes | yes | - |
| prefill ~4k: prompt tokens | 4040 | 4074 | - |
| prefill ~4k: TTFT | 4.98 s | 2.26 s | - |
| prefill ~4k: prompt tok/s | 811 | 1801 | - |
| prefill ~4k: code word found | yes | yes | - |
| prefill ~16k: prompt tokens | 16069 | 16060 | - |
| prefill ~16k: TTFT | 15.89 s | 8.32 s | - |
| prefill ~16k: prompt tok/s | 1011 | 1931 | - |
| prefill ~16k: code word found | yes | yes | - |
| prefill ~64k: prompt tokens | 64418 | 64124 | - |
| prefill ~64k: TTFT | 61.61 s | 32.88 s | - |
| prefill ~64k: prompt tok/s | 1046 | 1950 | - |
| prefill ~64k: code word found | yes | yes | - |
| prefill ~108k: prompt tokens | 108589 | 108709 | - |
| prefill ~108k: TTFT | 103.61 s | 56.58 s | - |
| prefill ~108k: prompt tok/s | 1048 | 1921 | - |
| prefill ~108k: code word found | yes | yes | - |
| cached-prefill 16k: TTFT | 1.541 s | 0.215 s | - |
| cached-prefill 16k: speedup vs uncached | 10.3x | 38.7x | - |
| parallel x2: aggregate tok/s | 21.3 | 73.2 | - |
| parallel x2: per-stream tok/s | 11.9 | 37.0 | - |
| parallel x4: aggregate tok/s | 22.7 | 171.1 | - |
| parallel x4: per-stream tok/s | 11.4 | 42.7 | - |

No speed section for: haiku.

## Ability (pass counts)

| category | hy4 | glm | haiku |
|---|---|---|---|
| reasoning | 7/8 | 8/8 | 7/8 |
| coding | 4/6 | 6/6 | 6/6 |
| json | 3/3 | 3/3 | 3/3 |
| tools | 4/4 | 4/4 | 4/4 |
| vision | 3/3 | 3/3 | 3/3 |
| longctx | 2/2 | 2/2 | 1/1 |
| instruct | 2/2 | 1/2 | 2/2 |
| thinking | 1/1 | 1/1 | 1/1 |
| **all** | **26/29** | **28/29** | **27/28** |
| all, tests every run has | 25/28 | 27/28 | 27/28 |

## Ability (per test)

| test | hy4 | glm | haiku |
|---|---|---|---|
| rsn-revenue | PASS (323 tok, 14 s) | PASS (170 tok, 2 s) | PASS |
| rsn-dice | PASS (243 tok, 9 s) | PASS (206 tok, 3 s) | PASS |
| rsn-logic-grid | PASS (1387 tok, 62 s) | PASS (1455 tok, 37 s) | PASS |
| rsn-date | PASS (1073 tok, 46 s) | PASS (664 tok, 10 s) | PASS |
| rsn-letter-count | PASS (483 tok, 19 s) | PASS (343 tok, 5 s) | PASS |
| rsn-reverse | PASS (819 tok, 32 s) | PASS (159 tok, 3 s) | fail |
| rsn-divisible-sum | PASS (407 tok, 16 s) | PASS (307 tok, 4 s) | PASS |
| rsn-pipes | fail (271 tok, 11 s) | PASS (199 tok, 3 s) | PASS |
| code-merge-intervals | PASS (1575 tok, 65 s) | PASS (363 tok, 6 s) | PASS |
| code-roman | PASS (1995 tok, 94 s) | PASS (820 tok, 15 s) | PASS |
| code-top-k-words | PASS (844 tok, 42 s) | PASS (255 tok, 5 s) | PASS |
| code-evaluate | fail (6000 tok, 186 s) | PASS (957 tok, 16 s) | PASS |
| code-lcs | PASS (1093 tok, 50 s) | PASS (487 tok, 8 s) | PASS |
| code-rate-limiter | fail (6000 tok, 311 s) | PASS (347 tok, 7 s) | PASS |
| json-customer | PASS (185 tok, 8 s) | PASS (43 tok, 1 s) | PASS |
| json-invoice | PASS (274 tok, 11 s) | PASS (156 tok, 3 s) | PASS |
| json-event | PASS (210 tok, 10 s) | PASS (74 tok, 1 s) | PASS |
| tool-weather | PASS (34 tok, 3 s) | PASS (20 tok, 1 s) | PASS |
| tool-calculator | PASS (22 tok, 1 s) | PASS (20 tok, 1 s) | PASS |
| tool-flights | PASS (142 tok, 6 s) | PASS (37 tok, 1 s) | PASS |
| tool-none | PASS (506 tok, 21 s) | PASS (3 tok, 1 s) | PASS |
| vision-shapes | PASS (97 tok, 5 s) | PASS (58 tok, 5 s) | PASS |
| vision-bars | PASS (77 tok, 4 s) | PASS (56 tok, 2 s) | PASS |
| vision-quadrants | PASS (56 tok, 3 s) | PASS (48 tok, 1 s) | PASS |
| longctx-30k | PASS (215 tok, 35 s) | PASS (182 tok, 20 s) | PASS |
| longctx-100k | PASS (272 tok, 100 s) | PASS (144 tok, 57 s) | - |
| if-five-vowels | PASS (236 tok, 12 s) | PASS (64 tok, 2 s) | PASS |
| if-twelve-words | PASS (269 tok, 12 s) | fail (78 tok, 1 s) | PASS |
| decode-thinking | PASS (1722 tok, 76 s) | PASS (2697 tok, 56 s) | PASS |

## HTML pages (not graded)

| page | hy4 | glm | haiku |
|---|---|---|---|
| html-solar-system | 12000 tok (12000 reasoning), 21.0 tok/s, 572 s, length, `no html` | 12000 tok (12000 reasoning), 41.3 tok/s, 291 s, length, `no html` | - |
| html-snake | 9817 tok (5922 reasoning), 22.8 tok/s, 431 s, stop, `hy4-snake.html` | 12000 tok (12000 reasoning), 41.0 tok/s, 293 s, length, `no html` | - |
| html-synth-landing | 12000 tok (12000 reasoning), 29.2 tok/s, 412 s, length, `no html` | 12000 tok (11637 reasoning), 41.6 tok/s, 289 s, length, `glm-synth-landing.html` | - |
