"""Hit rate against hand-checked domains. See README.md.

Public surface: `read_labels`, `judge`, `Label`, `Verdict`, `Report`.
"""

from scout.modules.benchmark._benchmark import Label, Report, Verdict, judge, read_labels

__all__ = ["Label", "Report", "Verdict", "judge", "read_labels"]
