"""Known hiring systems: detecting them and reading their public job feeds. See README.md.

Public surface: `Boards` (fetch a known board, guess one from a domain), `BoardsConfig`,
`Listing`, `BoardError`, and the pure `detect_boards`, `slug_candidates`, `name_matches`.
"""

from scout.modules.boards._detect import detect_boards
from scout.modules.boards._framework import BoardError, Listing
from scout.modules.boards._service import Boards, BoardsConfig
from scout.modules.boards._slugs import name_matches, slug_candidates

__all__ = [
    "BoardError",
    "Boards",
    "BoardsConfig",
    "Listing",
    "detect_boards",
    "name_matches",
    "slug_candidates",
]
