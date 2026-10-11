# USVFS on ARM64

MO2 2.5.2's USVFS injection stub calls Windows functions with a misaligned
stack. On the Steam Frame, programs started through MO2 fail before they can
use its virtual file system. ModSync offers a pinned backport of the
[stack-alignment fix](https://github.com/ModOrganizer2/usvfs/pull/93), so this
workaround does not depend on a future upstream release.

An instance installed with **Install MO2** on an ARM64 machine already has the
fix. MO2-LINT 7.0.3 and newer install ndabas's MO2 2.5.2-woa.1 there, which is
the official MO2 2.5.2 with these same four files. ModSync recognizes them and
reports the fix as installed, with nothing to restore. Apply is for instances
installed before that, or from the official MO2 archive.

## Apply or restore the fix

Choose the MO2 instance in ModSync. Close MO2, Skyrim, and all programs started
by MO2. On an ARM64 machine, **Apply ARM64 fix** appears in the dashboard's MO2
card and in the Steam launch hub when ModSync recognizes the original files.
The download is about 13 MB. This is an explicit action, and the fix remains
installed for launches through Steam as well as ModSync.

Before it changes anything, ModSync checks on the host whether any of your
processes has this instance's USVFS files loaded. Wine maps every DLL it loads
from its Linux path, so this catches MO2 and every game or tool MO2 started,
whatever its name. Another MO2 instance, or Steam waiting on the launch hub,
doesn't count.

ModSync only replaces the four USVFS files shipped with the official MO2 2.5.2
archive. It checks their SHA-256 hashes, the download's hash, and each replacement's
hash. A version number alone cannot distinguish the original from patched builds.
A manually patched DLL, a newer MO2 release, or any other unrecognized build is
left unchanged. An unrecognized build may already contain the fix.

The originals remain in `<instance>/.modsync-usvfs/backup/`. **Restore original
USVFS** puts them back, after verifying both the backup and the files it would
replace. It refuses to overwrite a different build installed since the fix.
Restoring also brings back the ARM64 launch failure. Neither the binaries nor
their backups sync to other machines.

The CLI operates on the instance chosen in ModSync:

```sh
modsync mo2 usvfs status
modsync mo2 usvfs apply
modsync mo2 usvfs restore
```

`modsync doctor` and the diagnostics bundle include the fix's status. If a
replacement was interrupted, run Apply again to finish it, or Restore to put
the originals back. The cache is verified on every use, so later applications
can run offline. Keep the backup directory for restoration.

This backport retains USVFS 0.5.6.1's statically linked VC++ runtime. It does
not introduce the separate prefix-runtime requirement of USVFS 0.5.7+.
MO2 itself still needs a current runtime, which ModSync checks when opening it.

## Pinned build and source

The binaries are ndabas's
[v0.5.6.1-woa.1 release](https://github.com/ndabas/usvfs/releases/tag/v0.5.6.1-woa.1).
ModSync downloads them from a byte-identical
[mirror on skjiisa/usvfs](https://github.com/skjiisa/usvfs/releases/tag/v0.5.6.1-woa.1),
a fork that also carries the source, and falls back to ndabas's release. Both
are checked against the same SHA-256, so the mirror can't change what gets
installed. The mirror keeps the fix available if the original release is
removed.
ModSync replaces the matching DLLs and proxy executables as a set:
`usvfs_x64.dll`, `usvfs_x86.dll`, `usvfs_proxy_x64.exe`, and `usvfs_proxy_x86.exe`.
The x64 proxy participates in injection as well as the DLL. Individual file
hashes are recorded in [`modsync/mo2/usvfs.py`](../modsync/mo2/usvfs.py).

- Source commit: `866ce70040c4fc34fd2ddb59b3567a97f20cd50e` in
  [ndabas/usvfs](https://github.com/ndabas/usvfs/tree/866ce70040c4fc34fd2ddb59b3567a97f20cd50e).
- Base commit: `9f7fd9660d51784aa2117cb45f2095e87312d558`.
- Archive: `usvfs_v0.5.6.1-woa.1.7z`.
- Archive SHA-256: `acfbdb928078686d3f5709fc57e416ebb524292174878a297f1b6cf8d3a88f8e`.
- Source archive: `usvfs-v0.5.6.1-woa.1-src.tar.gz` on the mirror release, the
  pinned commit with its asmjit, spdlog and udis86 submodules, made with
  `git archive`.
- License: [GPL-3.0](https://github.com/ndabas/usvfs/blob/866ce70040c4fc34fd2ddb59b3567a97f20cd50e/LICENSE).

The source change saves `rsp` in `rbp` after saving the registers, aligns `rsp`
to 16 bytes, and restores `rsp` before restoring registers on both exit paths.
It also adds the `-woa` version suffix and a release workflow. It backports
commit `8577455` from USVFS PR #93.

The source includes
[the complete build workflow](https://github.com/ndabas/usvfs/blob/866ce70040c4fc34fd2ddb59b3567a97f20cd50e/.github/workflows/release-backport.yml).
To rebuild, check out the pinned commit with submodules on Windows with Visual
Studio 2022, obtain the Boost 1.85.0 prebuilt dependency listed in that workflow,
and run its MSBuild commands for x64 and x86. Compiler versions and timestamps
can change the output, so inspect and test rebuilt binaries before updating
ModSync's pinned hashes.

## Validation

Tested on a Steam Frame (SteamOS, aarch64, 4 KiB pages) on 2026-10-03 with the
aarch64 Flatpak built from this change, Proton 11.0 (ARM64) in the Steam Linux
Runtime 4, and an MO2 2.5.2 instance installed by MO2-LINT with its four
original USVFS files:

- **Game through MO2.** With the backport applied, Steam's Play button, the
  launch hub's Continue, and MO2's Run started Skyrim with a plugin from an
  MO2 mod. The usvfs log showed `inithooks in process <pid> successful` for
  `SkyrimSE.exe`, and a new game started in that mod's alternate start.
  Explore Virtual Folder (Explorer++) was hooked the same way and listed the
  mods' files in `Data`.
- **Prefix.** Nothing beyond what Open MO2 and Play already do. Like the
  original, the backport imports only Windows system DLLs, not the VC++
  runtime, and ModSync doesn't offer the USVFS 0.5.7+ prefix runtime for it.
- **In-use check.** Under Proton ARM64, Wine maps `usvfs_x64.dll` from its
  Linux path into MO2 and every program it starts, so the check sees them
  from inside the Flatpak. Apply and Restore were refused while MO2 ran, while
  Explorer++ or Skyrim was still running after MO2 had been closed, and while
  both ran. They were allowed once everything had exited. Wine processes
  that stay behind without USVFS, such as `nxmhandler.exe`, don't block it.
- **Launch hub.** Apply (with the download), Restore, and Apply from the
  cache worked while the hub was open. Continue and Cancel were disabled
  during each change.
- **Restore and cache.** Restore put back files matching the backup. A
  second Restore reported that the originals were in place and changed
  nothing. With the network disconnected, Apply reinstalled the fix from the
  cache. Without a cache, it reported the download failure and left the
  instance unchanged.

The offline suite covers corrupt downloads and backups, unknown builds,
interrupted replacements, rollback, restoration after updates, host process
checks against real processes, and GUI actions.
