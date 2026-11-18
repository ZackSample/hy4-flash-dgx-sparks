# Benchmark: haiku (haiku)

Started 2026-11-18T00:59:01, finished 2026-11-18T00:59:02, Offline answers graded from `bench/results/offline`.


## Ability

| category | pass | fail | error | unsupported | no answer |
|---|---|---|---|---|---|
| reasoning | 7/8 | 1 | 0 | 0 | 0 |
| coding | 6/6 | 0 | 0 | 0 | 0 |
| json | 3/3 | 0 | 0 | 0 | 0 |
| tools | 4/4 | 0 | 0 | 0 | 0 |
| vision | 3/3 | 0 | 0 | 0 | 0 |
| longctx | 1/1 | 0 | 0 | 0 | 0 |
| instruct | 2/2 | 0 | 0 | 0 | 0 |
| thinking | 1/1 | 0 | 0 | 0 | 0 |
| **all** | **27/28** | | | | |

| test | status | expected | got | tokens (reasoning) | time s |
|---|---|---|---|---|---|
| rsn-revenue | pass | 538.75 | 538.75 | - (-) | - |
| rsn-dice | pass | 5/18 | 5/18 | - (-) | - |
| rsn-logic-grid | pass | ["Ana", "Dev", "Cleo", "Ben"] | Ana, Dev, Cleo, Ben | - (-) | - |
| rsn-date | pass | 2025-12-09 | 2025-12-09 | - (-) | - |
| rsn-letter-count | pass | 14 | 14 | - (-) | - |
| rsn-reverse | fail | hsalF-6202kramhcneB |  | - (-) | - |
| rsn-divisible-sum | pass | 53679 | 53679 | - (-) | - |
| rsn-pipes | pass | 36/7 | 36/7 | - (-) | - |
| code-merge-intervals | pass | all hidden tests pass | 10/10 tests | - (-) | - |
| code-roman | pass | all hidden tests pass | 3/3 tests | - (-) | - |
| code-top-k-words | pass | all hidden tests pass | 8/8 tests | - (-) | - |
| code-evaluate | pass | all hidden tests pass | 4/4 tests | - (-) | - |
| code-lcs | pass | all hidden tests pass | 3/3 tests | - (-) | - |
| code-rate-limiter | pass | all hidden tests pass | 6/6 tests | - (-) | - |
| json-customer | pass | {"name": "Priya Raman-Okafor", "age": 41, "emai... | ok | - (-) | - |
| json-invoice | pass | {"invoice_number": "INV-2027-0042", "issue_date... | ok | - (-) | - |
| json-event | pass | {"date": "2027-04-15", "start_time": "14:15", "... | ok | - (-) | - |
| tool-weather | pass | get_weather | {"city": "Tokyo", "unit": "fahrenheit"} | - (-) | - |
| tool-calculator | pass | calculator | {"expression": "1234 * 5678 + 91"} | - (-) | - |
| tool-flights | pass | search_flights | {"origin": "Boston", "destination": "Denver", "date": "2027-03-03",... | - (-) | - |
| tool-none | pass | no tool call | no tool call | - (-) | - |
| vision-shapes | pass | [3, 2] | 3, 2 | - (-) | - |
| vision-bars | pass | 3 | 3 | - (-) | - |
| vision-quadrants | pass | green | green | - (-) | - |
| longctx-30k | pass | 1621 | 1621 | - (-) | - |
| if-five-vowels | pass | rule satisfied | AEIOU | - (-) | - |
| if-twelve-words | pass | rule satisfied | 12 words | - (-) | - |
| decode-thinking | pass | ["E"] | E | - (-) | - |
