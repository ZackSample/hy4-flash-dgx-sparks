# Benchmark: hy4 (hy4-flash)

Started 2026-11-18T03:49:07, finished 2026-11-18T04:47:19, base `http://localhost:8888/v1`, think-off `{"chat_template_kwargs": {"reasoning_effort": "no_think"}}`, think-on `{"chat_template_kwargs": {"reasoning_effort": "high"}}`.

## Speed

| test | ttft s | tokens | tok/s | total s | notes |
|---|---|---|---|---|---|
| decode-prose (median of 3/3) | 0.45 | 1000 | 14.8 | 67.8 | |
| decode-code (median of 3/3) | 0.69 | 1500 | 29.7 | 51.3 | |
| decode-thinking | 1.52 | 1722 (reasoning 1318, answer 404) | 23.1 | 76.3 | correct=yes  |
| prefill ~4k | 4.98 | prompt 4040 | 811 (prompt) | 5.1 | found=yes  |
| prefill ~16k | 15.89 | prompt 16069 | 1011 (prompt) | 16.0 | found=yes  |
| prefill ~64k | 61.61 | prompt 64418 | 1046 (prompt) | 61.7 | found=yes  |
| prefill ~108k | 103.61 | prompt 108589 | 1048 (prompt) | 103.7 | found=yes  |
| cached-prefill 16k | 1.54 | prompt 16066 | uncached ttft 15.89 s, speedup 10.3x | 1.7 | found=yes  |
| parallel x2 | - | 1796 | aggregate 21.3, per-stream 11.9 | 85.5 | ok 2/2 |
| parallel x4 | - | 3801 | aggregate 22.7, per-stream 11.4 | 168.1 | ok 4/4 |

## Ability

| category | pass | fail | error | unsupported | no answer |
|---|---|---|---|---|---|
| reasoning | 7/8 | 1 | 0 | 0 | 0 |
| coding | 4/6 | 2 | 0 | 0 | 0 |
| json | 3/3 | 0 | 0 | 0 | 0 |
| tools | 4/4 | 0 | 0 | 0 | 0 |
| vision | 3/3 | 0 | 0 | 0 | 0 |
| longctx | 2/2 | 0 | 0 | 0 | 0 |
| instruct | 2/2 | 0 | 0 | 0 | 0 |
| **all** | **25/28** | | | | |

| test | status | expected | got | tokens (reasoning) | time s |
|---|---|---|---|---|---|
| rsn-revenue | pass | 538.75 | 538.75 | 323 (233) | 13.8 |
| rsn-dice | pass | 5/18 | 5/18 | 243 (159) | 9.4 |
| rsn-logic-grid | pass | ["Ana", "Dev", "Cleo", "Ben"] | Ana, Dev, Cleo, Ben | 1387 (1219) | 62.3 |
| rsn-date | pass | 2025-12-09 | 2025-12-09 | 1073 (989) | 45.6 |
| rsn-letter-count | pass | 14 | 14 | 483 (432) | 19.4 |
| rsn-reverse | pass | hsalF-6202kramhcneB | hsalF-6202kramhcneB | 819 (803) | 32.0 |
| rsn-divisible-sum | pass | 53679 | 53679 | 407 (275) | 15.8 |
| rsn-pipes | fail | 36/7 | \frac{36}{7} | 271 (134) | 11.1 |
| code-merge-intervals | pass | all hidden tests pass | 10/10 tests | 1575 (1401) | 64.7 |
| code-roman | pass | all hidden tests pass | 3/3 tests | 1995 (1738) | 93.8 |
| code-top-k-words | pass | all hidden tests pass | 8/8 tests | 844 (756) | 41.6 |
| code-evaluate | fail | all hidden tests pass | no code block | 6000 (6000) | 186.4 |
| code-lcs | pass | all hidden tests pass | 3/3 tests | 1093 (795) | 50.1 |
| code-rate-limiter | fail | all hidden tests pass | no code block | 6000 (6000) | 311.2 |
| json-customer | pass | {"name": "Priya Raman-Okafor", "age": 41, "emai... | ok | 185 (151) | 7.9 |
| json-invoice | pass | {"invoice_number": "INV-2027-0042", "issue_date... | ok | 274 (202) | 10.8 |
| json-event | pass | {"date": "2027-04-15", "start_time": "14:15", "... | ok | 210 (168) | 9.7 |
| tool-weather | pass | get_weather | {"city": "Tokyo", "unit": "fahrenheit"} | 34 (0) | 2.7 |
| tool-calculator | pass | calculator | {"expression": "1234 * 5678 + 91"} | 22 (0) | 1.4 |
| tool-flights | pass | search_flights | {"origin": "Boston", "destination": "Denver", "date": "2027-03-03",... | 142 (102) | 5.6 |
| tool-none | pass | no tool call | no tool call | 506 (503) | 21.3 |
| vision-shapes | pass | [3, 2] | 3, 2 | 97 (75) | 5.1 |
| vision-bars | pass | 3 | 3 | 77 (62) | 4.1 |
| vision-quadrants | pass | green | green | 56 (42) | 3.0 |
| longctx-30k | pass | 801 | 801 | 215 (168) | 35.1 |
| longctx-100k | pass | 1982 | 1982 | 272 (223) | 100.4 |
| if-five-vowels | pass | rule satisfied | AEIOU | 236 (189) | 12.5 |
| if-twelve-words | pass | rule satisfied | 12 words | 269 (253) | 11.9 |

## HTML

| page | file | tokens (reasoning) | tok/s | time s | finish |
|---|---|---|---|---|---|
| html-solar-system | none | 12000 (12000) | 21.0 | 572.2 | length |
| html-snake | html/hy4-snake.html | 9817 (5922) | 22.8 | 430.6 | stop |
| html-synth-landing | none | 12000 (12000) | 29.2 | 411.8 | length |
