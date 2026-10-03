# benchmark

Scores scout against hand-checked labels (`benchmark/labels.csv`). `judge(label, result)` is
pure and decides one hit: the right kind of answer (the board system and slug, the custom
careers page with language variants treated as equal, or no careers page) and, where the label
counts jobs, a job count within max(2, 25%) of it, since ads open and close between labelling
and running. `Report` gives the hit rate, hits per `found_by`, and miss reasons, which say which
discovery step to improve next.

Only labels with `checked_by` filled in are Rasmus-checked; `scout bench` reports drafted and
checked labels separately and treats only the checked ones as the real number.
