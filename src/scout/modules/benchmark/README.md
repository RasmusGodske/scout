# benchmark

Scores scout against hand-checked labels (`benchmark/labels.csv`: 40 European companies not
from any lead list and not used while developing scout). `judge(label, result, supported)` is
pure and decides one hit: the right kind of answer (the board, for a hiring system scout has an
adapter for, keyed by its slug or the company host it is served on; the careers page for a
custom page or an unsupported hiring system, language variants and pages above it treated as
equal; or no careers page) and, where the label counts jobs, a job count within max(2, 25%).

Where a label lists every job's URL (custom pages), or says there are none, the benchmark also
counts per job: real jobs found, reported "jobs" that are not real, real jobs missed. `Report`
gives the company hit rate, **job precision** (reported jobs that are real) and **job recall**
(real jobs found), hits per `found_by`, and miss reasons.

Only labels with `checked_by` filled in are Rasmus-checked; `scout bench` reports drafted and
checked labels separately and treats only the checked ones as the real number.
