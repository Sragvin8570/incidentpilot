# Evaluation summary

LLM calls count successful model responses, including investigation/tool turns and RCA or validation retries; Kubernetes tool invocations are not LLM calls.

## full (1 valid, 1 invalid)

| Metric | Mean / rate |
|---|---:|
| Invalid runs | 1 |
| RCA correct | 100.0% |
| Action correct | 100.0% |
| Final state correct | 100.0% |
| Hallucinated claims | 0.00 |
| Time to final state (s) | 755.7 |
| LLM calls | 4.0 |
| Prompt tokens | 20166.0 |
| Completion tokens | 1727.0 |
| False resolutions | 0.0% |

## no_tools (0 valid, 0 invalid)

| Metric | Mean / rate |
|---|---:|
| Invalid runs | 0 |
| RCA correct | n/a |
| Action correct | n/a |
| Final state correct | n/a |
| Hallucinated claims | n/a |
| Time to final state (s) | n/a |
| LLM calls | n/a |
| Prompt tokens | n/a |
| Completion tokens | n/a |
| False resolutions | n/a |

## no_verification (0 valid, 0 invalid)

| Metric | Mean / rate |
|---|---:|
| Invalid runs | 0 |
| RCA correct | n/a |
| Action correct | n/a |
| Final state correct | n/a |
| Hallucinated claims | n/a |
| Time to final state (s) | n/a |
| LLM calls | n/a |
| Prompt tokens | n/a |
| Completion tokens | n/a |
| False resolutions | n/a |
