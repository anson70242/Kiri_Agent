"""The pipeline's side of the cloud drive: the one call it makes, what comes back, and the
two ways it fails. The drive itself (Quark, through its CLI) implements the call; see
docs/adr/0001-quark-drive-through-the-official-agent-cli.md.

    drive(archive, folder, progress) -> DriveCopy

uploads the archive's drive copy into `folder` (a path from the top of the drive, like
`录播/某主播`; created when missing) and makes its drive link. `progress(sent, total)`
reports the bytes sent so far and the zip's size (None while not known). No drive at all
(no CLI, no authorization yet) is the pipeline holding no call: "drive not available".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.archive import Archive


@dataclass(frozen=True)
class DriveCopy:
    """An archive's drive copy, uploaded (or found already there)."""

    path: str  # the zip's path on the drive: `录播/某主播/[20261118][某主播] 标题.zip`
    link: str | None  # the drive link, extraction code inside; None when it could not be made
    reason: str = ""  # without a link: why there is none


class DriveAuthorizationLapsed(Exception):
    """The drive's authorization has lapsed: nothing was uploaded until the owner renews it."""


class DriveFailed(Exception):
    """The upload failed; the message says why (the drive's own words when it gave any)."""


# What the owner runs on the server to authorize the drive again (told when it lapsed).
LOGIN_COMMAND = "docker compose exec kiri /app/.venv/bin/python -m backend.quark login"

DriveProgress = Callable[[int, int | None], None]  # (bytes sent, zip size or None)
Drive = Callable[[Archive, str, DriveProgress], DriveCopy]
