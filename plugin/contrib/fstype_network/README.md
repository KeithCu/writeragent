# Vendored systemd `fstype_is_network` (LGPL-2.1-or-later)

Faithful Python transcription of `fstype_is_network` in
[systemd `mountpoint-util.c`](https://github.com/systemd/systemd/blob/ca78951bd398e191ebc4550710e33ef34190514e/src/basic/mountpoint-util.c)
(commit `ca78951bd398e191ebc4550710e33ef34190514e`).

Network filesystem names come from the `@network` set in
[systemd `filesystem-sets.py`](https://github.com/systemd/systemd/blob/ca78951bd398e191ebc4550710e33ef34190514e/src/basic/filesystem-sets.py)
plus the four extra names appended in the C function (`davfs`, `glusterfs`, `lustre`, `sshfs`).

WriterAgent adds: `9p`, `9pnet`, `smb`, `rclone`, `gvfsd-fuse`, `blobfuse`.

Not vendored: `path_is_network_fs`, `is_network_fs`, or magic-number classification.
