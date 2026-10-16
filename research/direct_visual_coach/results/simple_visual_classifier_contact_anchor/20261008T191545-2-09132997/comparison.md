# Contact-anchor comparison

Descriptive comparison only. Neither result is ground truth, and neither is marked as the winner.

## False early after_strike

Disappeared: True
Old ticks before contact classified after_strike: [31, 33, 35, 37, 40]
New ticks before contact classified after_strike: []

## Faults

Removed: ['STEEP_DELIVERY']
Added: ['DELIVERY_POSTURE_RISE', 'FINISH_BALANCE_LOST']
EARLY_RELEASE: confidence 0.5 -> 0.5; ticks [27, 29] -> [37]
HAND_PATH_OUT: confidence 0.88 -> 0.8; ticks [25, 26, 28] -> [37, 40]
OUT_TO_IN_DELIVERY: confidence 0.75 -> 0.7; ticks [27, 29] -> [37, 40]

## Runs

### OLD SIMPLE CLASSIFIER
latency_seconds: 10.717999999877065
input_tokens: 47649
output_tokens: 1140
estimated_cost_usd: 0.01419225

### CONTACT-ANCHORED CLASSIFIER
latency_seconds: 11.657000000122935
input_tokens: 48075
output_tokens: 1097
estimated_cost_usd: 0.01421275
