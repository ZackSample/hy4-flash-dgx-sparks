# Offline ability tests

No system prompt is used; each `<test-id>.prompt.txt` is the complete user message.
For each test, write the model's complete reply to `<test-id>.answer.txt` in this folder.
Vision tests: the image `<test-id>.png` is attached to the prompt (image after the text).
Tool tests: the prompt file also lists the tools and asks for one JSON object `{"tool": name-or-null, "arguments": {...}, "answer": text-or-null}` as the whole reply.

| # | test id | category | prompt | image | answer file to write |
|---|---|---|---|---|---|
| 1 | rsn-revenue | reasoning | rsn-revenue.prompt.txt | - | rsn-revenue.answer.txt |
| 2 | rsn-dice | reasoning | rsn-dice.prompt.txt | - | rsn-dice.answer.txt |
| 3 | rsn-logic-grid | reasoning | rsn-logic-grid.prompt.txt | - | rsn-logic-grid.answer.txt |
| 4 | rsn-date | reasoning | rsn-date.prompt.txt | - | rsn-date.answer.txt |
| 5 | rsn-letter-count | reasoning | rsn-letter-count.prompt.txt | - | rsn-letter-count.answer.txt |
| 6 | rsn-reverse | reasoning | rsn-reverse.prompt.txt | - | rsn-reverse.answer.txt |
| 7 | rsn-divisible-sum | reasoning | rsn-divisible-sum.prompt.txt | - | rsn-divisible-sum.answer.txt |
| 8 | rsn-pipes | reasoning | rsn-pipes.prompt.txt | - | rsn-pipes.answer.txt |
| 9 | code-merge-intervals | coding | code-merge-intervals.prompt.txt | - | code-merge-intervals.answer.txt |
| 10 | code-roman | coding | code-roman.prompt.txt | - | code-roman.answer.txt |
| 11 | code-top-k-words | coding | code-top-k-words.prompt.txt | - | code-top-k-words.answer.txt |
| 12 | code-evaluate | coding | code-evaluate.prompt.txt | - | code-evaluate.answer.txt |
| 13 | code-lcs | coding | code-lcs.prompt.txt | - | code-lcs.answer.txt |
| 14 | code-rate-limiter | coding | code-rate-limiter.prompt.txt | - | code-rate-limiter.answer.txt |
| 15 | json-customer | json | json-customer.prompt.txt | - | json-customer.answer.txt |
| 16 | json-invoice | json | json-invoice.prompt.txt | - | json-invoice.answer.txt |
| 17 | json-event | json | json-event.prompt.txt | - | json-event.answer.txt |
| 18 | tool-weather | tools | tool-weather.prompt.txt | - | tool-weather.answer.txt |
| 19 | tool-calculator | tools | tool-calculator.prompt.txt | - | tool-calculator.answer.txt |
| 20 | tool-flights | tools | tool-flights.prompt.txt | - | tool-flights.answer.txt |
| 21 | tool-none | tools | tool-none.prompt.txt | - | tool-none.answer.txt |
| 22 | vision-shapes | vision | vision-shapes.prompt.txt | vision-shapes.png | vision-shapes.answer.txt |
| 23 | vision-bars | vision | vision-bars.prompt.txt | vision-bars.png | vision-bars.answer.txt |
| 24 | vision-quadrants | vision | vision-quadrants.prompt.txt | vision-quadrants.png | vision-quadrants.answer.txt |
| 25 | longctx-30k | longctx | longctx-30k.prompt.txt | - | longctx-30k.answer.txt |
| 26 | if-five-vowels | instruct | if-five-vowels.prompt.txt | - | if-five-vowels.answer.txt |
| 27 | if-twelve-words | instruct | if-twelve-words.prompt.txt | - | if-twelve-words.answer.txt |
| 28 | decode-thinking | thinking | decode-thinking.prompt.txt | - | decode-thinking.answer.txt |
