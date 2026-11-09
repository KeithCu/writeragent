# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Transcribed from systemd src/basic/mountpoint-util.c (fstype_is_network).
# Upstream: https://github.com/systemd/systemd/blob/ca78951bd398e191ebc4550710e33ef34190514e/src/basic/mountpoint-util.c
# @network names: https://github.com/systemd/systemd/blob/ca78951bd398e191ebc4550710e33ef34190514e/src/basic/filesystem-sets.py
from __future__ import annotations

# FILESYSTEM_SET_NETWORK (@network) plus davfs, glusterfs, lustre, sshfs from mountpoint-util.c,
# plus WriterAgent names for mounts not in upstream's string helper.
_NETWORK_FSTYPES: frozenset[str] = frozenset(
    {
        "afs",
        "ceph",
        "cifs",
        "gfs",
        "gfs2",
        "ncp",
        "ncpfs",
        "nfs",
        "nfs4",
        "ocfs2",
        "orangefs",
        "pvfs2",
        "smb3",
        "smbfs",
        "davfs",
        "glusterfs",
        "lustre",
        "sshfs",
        "9p",
        "9pnet",
        "smb",
        "rclone",
        "gvfsd-fuse",
        "blobfuse",
    }
)
_NETWORK_FSTYPES_FOLDED: frozenset[str] = frozenset(name.casefold() for name in _NETWORK_FSTYPES)


def fstype_is_network(fstype: str | None) -> bool:
    """Return True when *fstype* names a network filesystem (systemd transcription).

    Strips a leading ``fuse.`` before matching, so ``fuse.sshfs`` matches ``sshfs``.
    Unknown or empty types return False (fail-open toward local / WAL).
    """
    if not fstype or not fstype.strip():
        return False
    name = fstype.strip()
    lowered = name.casefold()
    if lowered.startswith("fuse."):
        name = name[len("fuse.") :]
        lowered = name.casefold()
    return lowered in _NETWORK_FSTYPES_FOLDED
