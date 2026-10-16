# Camera budget matrix 20261008T191545-2-09132997 20261009T105559Z

No configuration is marked best. The matrix has no human ground truth. Agreement is descriptive and is not fed back into any request or into CaddieJ.

| configuration | camera_mode | image_budget | temporal_moments | image_count | selected_ticks | status | handedness | orientation_valid | fault_candidates | primary_fault | primary_confidence | input_tokens | output_tokens | openai_latency_s | estimated_cost_usd |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DTL_SMALL | DTL | SMALL | 8 | 8 | 15 19 24 27 35 43 50 51 | no_proven_fault | right | True | - | - | - | 23894 | 524 | 5.656 | 0.0070215 |
| DTL_MEDIUM | DTL | MEDIUM | 16 | 16 | 15 17 19 20 22 24 26 28 30 33 36 37 41 44 47 51 | no_proven_fault | right | True | - | - | - | 38662 | 610 | 5.766 | 0.0108855 |
| DTL_LARGE | DTL | LARGE | 24 | 24 | 15 16 17 18 20 21 22 23 24 25 26 27 28 29 31 33 35 37 40 42 44 47 49 51 | no_proven_fault | right | True | - | - | - | 53430 | 1260 | 11.547 | 0.0158775 |
| FO_SMALL | FO | SMALL | 8 | 8 | 15 19 23 27 35 41 42 51 | no_proven_fault | right | True | - | - | - | 27496 | 358 | 5.86 | 0.00759 |
| FO_MEDIUM | FO | MEDIUM | 16 | 16 | 15 17 19 20 22 24 26 28 30 33 37 41 43 44 47 51 | no_proven_fault | right | True | - | - | - | 45864 | 588 | 7.781 | 0.012642 |
| FO_LARGE | FO | LARGE | 24 | 24 | 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 31 33 37 40 42 44 47 49 51 | no_proven_fault | right | True | - | - | - | 64232 | 858 | 11.187 | 0.017774 |
| BOTH_SMALL | BOTH | SMALL | 4 | 8 | 15 24 37 51 | no_proven_fault | right | True | - | - | - | 25571 | 237 | 3.625 | 0.00686675 |
| BOTH_MEDIUM | BOTH | MEDIUM | 8 | 16 | 15 19 24 27 35 41 42 51 | no_proven_fault | right | True | - | - | - | 42055 | 384 | 4.704 | 0.01128175 |
| BOTH_LARGE | BOTH | LARGE | 12 | 24 | 15 18 20 23 25 28 31 36 37 41 46 51 | invalid | right | False | - | - | - | 58539 | 531 | 6.64 | 0.01569675 |

## Aggregates

- fastest: configuration=BOTH_SMALL, openai_latency_s=3.625
- lowest input tokens: configuration=DTL_SMALL, input_tokens=23894, source=measured
- lowest cost: configuration=BOTH_SMALL, estimated_cost_usd=0.00686675
- no_proven_fault: DTL_SMALL, DTL_MEDIUM, DTL_LARGE, FO_SMALL, FO_MEDIUM, FO_LARGE, BOTH_SMALL, BOTH_MEDIUM
- one or more proven faults: -

## Agreement

No validated replies yet.

## Agreement by camera mode

- DTL: -
- FO: -
- BOTH: -

## Agreement by image budget

- SMALL: -
- MEDIUM: -
- LARGE: -
