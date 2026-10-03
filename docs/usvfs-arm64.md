# USVFS on ARM64

MO2 2.5.2's USVFS injection stub calls Windows functions with a misaligned
stack. On the Steam Frame, programs started through MO2 fail before they can
use its virtual file system. ModSync offers a pinned backport of the
[stack-alignment fix](https://github.com/ModOrganizer2/usvfs/pull/93), so this
workaround does not depend on a future upstream release.

## Apply or restore the fix

Choose the MO2 instance in ModSync. Close MO2, Skyrim, and all programs started
by MO2. On an ARM64 machine, **Apply ARM64 fix** appears in the dashboard's MO2
card and in the Steam launch hub when ModSync recognizes the original files.
The download is about 13 MB. This is an explicit action, and the fix remains
installed for launches through Steam as well as ModSync.

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

The binaries come from ndabas's
[v0.5.6.1-woa.1 release](https://github.com/ndabas/usvfs/releases/tag/v0.5.6.1-woa.1).
ModSync replaces the matching DLLs and proxy executables as a set:
`usvfs_x64.dll`, `usvfs_x86.dll`, `usvfs_proxy_x64.exe`, and `usvfs_proxy_x86.exe`.
The x64 proxy participates in injection as well as the DLL. Individual file
hashes are recorded in [`modsync/mo2/usvfs.py`](../modsync/mo2/usvfs.py).

- Source commit: `866ce70040c4fc34fd2ddb59b3567a97f20cd50e` in
  [ndabas/usvfs](https://github.com/ndabas/usvfs/tree/866ce70040c4fc34fd2ddb59b3567a97f20cd50e).
- Base commit: `9f7fd9660d51784aa2117cb45f2095e87312d558`.
- Archive: `usvfs_v0.5.6.1-woa.1.7z`.
- Archive SHA-256: `acfbdb928078686d3f5709fc57e416ebb524292174878a297f1b6cf8d3a88f8e`.
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

The stack-alignment fix was tested on the Steam Frame in
[ModSync #62](https://github.com/skjiisa/ModSync/issues/62), using another build.
The exact backport selected here still needs a Frame test through ModSync.

The offline suite covers corrupt downloads and backups, unknown builds,
interrupted replacements, rollback, restoration after updates, host process
checks, and GUI actions. A local check also exercises extraction, installation,
and restoration using the actual release binaries, without executing them.

For the hardware check, use an unmodified MO2 2.5.2 instance on a Steam Frame:

1. Apply the fix from the aarch64 Flatpak and confirm the dashboard and
   `doctor` show it installed.
2. Launch through Steam, open MO2's virtual folder, and start Skyrim with a
   real mod. Confirm the game sees that mod.
3. Close MO2 and its programs, restore the originals, and verify their hashes
   match the backup. Reapply the fix with the network disconnected.
4. Check the controls from the Steam launch hub and verify that the hub cannot
   close or continue a launch during replacement.
