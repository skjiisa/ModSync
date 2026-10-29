# USVFS on ARM64

MO2 2.5.2's USVFS has two problems on the Steam Frame, and ModSync offers a
pinned build that fixes both, so this workaround does not depend on a future
upstream release:

1. Its injection stub calls Windows functions with a misaligned stack, so
   programs started through MO2 fail before they can use its virtual file
   system. The [stack-alignment fix](https://github.com/ModOrganizer2/usvfs/pull/93)
   is backported in `v0.5.6.1-woa.1`.
2. A program MO2 starts doesn't pass USVFS on to the programs *it* starts. When
   MO2 runs SKSE's loader, the game the loader starts sees only the real Data
   folder, so it runs without your mods. `v0.5.6.1-woa.2` fixes this; see
   [Programs started by programs](#programs-started-by-programs).

An instance installed with **Install MO2** on an ARM64 machine has the first
fix only. MO2-LINT 7.0.3 and newer install ndabas's MO2 2.5.2-woa.1 there,
which is the official MO2 2.5.2 with the four woa.1 USVFS files. ModSync
recognizes them and offers **Update ARM64 fix**. Apply is for instances
installed before that, or from the official MO2 archive.

## Programs started by programs

USVFS sees a new process through its hook on `CreateProcessInternalW` in
`kernelbase.dll`: it starts the process suspended, injects itself and resumes
it. Proton's ARM64 builds of `kernel32.dll` and `kernelbase.dll` are ARM64X
files, so x64 programs run their ARM64EC code natively. Hooking a function
there only patches the x64 code reached through its export.
`CreateProcessA` and `CreateProcessW` reach `CreateProcessInternalW` from
native code inside `kernelbase.dll`, past the hook. MO2 itself injects what it
starts directly, which is why the game ran with mods when MO2 started
`SkyrimSE.exe`, and why Explore Virtual Folder worked. But SKSE's loader calls
`CreateProcessA`, and nothing injected the game it started. USVFS's file hooks
in `ntdll.dll` are still reached this way.

woa.2 also hooks `CreateProcessA` and `CreateProcessW` at their exports and
sends both through the existing `CreateProcessInternalW` hook, which reroutes,
injects and resumes as before. The ANSI version converts its strings as
`CreateProcessInternalA` does. Where the real `CreateProcessInternalW` is still
reached through its own hook (x86 and x64 Windows, and Wine on x86_64), that
inner call finds the hook's process-creation group already taken and passes
through, so a child is injected once. `CreateProcessAsUserA/W`, direct callers
of `CreateProcessInternalA`, `WinExec` and `ShellExecute` aren't covered.

## Apply or restore the fix

Choose the MO2 instance in ModSync. Close MO2, Skyrim, and all programs started
by MO2. On an ARM64 machine, Home suggests **Apply ARM64 fix** as the next
step when ModSync recognizes the original files, and **Update ARM64 fix** when
it recognizes woa.1. It does this also when Steam's Play button opened ModSync.
Both are also in the Mod Organizer 2 sheet, opened from the Mod Organizer 2 row
on Home.
The download is about 13 MB. This is an explicit action, and the fix remains
installed for launches through Steam as well as ModSync.

Before it changes anything, ModSync checks on the host whether any of your
processes has this instance's USVFS files loaded. Wine maps every DLL it loads
from its Linux path, so this catches MO2 and every game or tool MO2 started,
whatever its name. Another MO2 instance, or Steam waiting on the launch hub,
doesn't count.

ModSync only replaces the four USVFS files shipped with the official MO2 2.5.2
archive, or the four woa.1 files. It checks their SHA-256 hashes, the download's hash, and each replacement's
hash. A version number alone cannot distinguish the original from patched builds.
A manually patched DLL, a newer MO2 release, or any other unrecognized build is
left unchanged. An unrecognized build may already contain the fix.

The originals remain in `<instance>/.modsync-usvfs/backup/`. **Restore original
USVFS** puts them back, after verifying both the backup and the files it would
replace. Updating from woa.1 keeps a backup that's already there, and never
saves woa.1 as one: an instance that started with woa.1 has no originals, so
it has nothing to restore. It refuses to overwrite a different build installed since the fix.
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
the originals back. An interrupted update from woa.1 with no originals can
only be finished. The cache is verified on every use, so later applications
can run offline. Keep the backup directory for restoration.

This backport retains USVFS 0.5.6.1's statically linked VC++ runtime. It does
not introduce the separate prefix-runtime requirement of USVFS 0.5.7+.
MO2 itself still needs a current runtime, which ModSync checks when opening it.

## Pinned build and source

The binaries are the
[v0.5.6.1-woa.2 release](https://github.com/skjiisa/usvfs/releases/tag/v0.5.6.1-woa.2)
of skjiisa/usvfs, ModSync's fork of ndabas's USVFS backport. It adds the
`CreateProcessA`/`CreateProcessW` hooks to ndabas's
[v0.5.6.1-woa.1](https://github.com/ndabas/usvfs/releases/tag/v0.5.6.1-woa.1).
Its archive was built by the fork's release workflow from the tagged commit,
and those same bytes were tested on a Steam Frame. ModSync downloads it from
that release only, because ndabas's repository has no woa.2, and checks it
against the pinned SHA-256.
ModSync replaces the matching DLLs and proxy executables as a set:
`usvfs_x64.dll`, `usvfs_x86.dll`, `usvfs_proxy_x64.exe`, and `usvfs_proxy_x86.exe`.
The x64 proxy participates in injection as well as the DLL. Individual file
hashes, including woa.1's, are recorded in [`modsync/mo2/usvfs.py`](../modsync/mo2/usvfs.py).

- Source commit: `b06d1a9660e5c8eada5644b3dcb57ee2346cb84a` in
  [skjiisa/usvfs](https://github.com/skjiisa/usvfs/tree/b06d1a9660e5c8eada5644b3dcb57ee2346cb84a).
- Based on woa.1: `866ce70040c4fc34fd2ddb59b3567a97f20cd50e` in
  [ndabas/usvfs](https://github.com/ndabas/usvfs/tree/866ce70040c4fc34fd2ddb59b3567a97f20cd50e),
  itself on USVFS `9f7fd9660d51784aa2117cb45f2095e87312d558`.
- Archive: `usvfs_v0.5.6.1-woa.2.7z`.
- Archive SHA-256: `123abba9f63d6840978a3a03416d0a0be4c9aaa0f0f80ad1d1e73b5d82dcd04f`.
- Source archive: `usvfs-v0.5.6.1-woa.2-src.tar.gz` on the release, the
  pinned commit with its asmjit, spdlog and udis86 submodules, made with
  `git archive`.
- License: [GPL-3.0](https://github.com/skjiisa/usvfs/blob/b06d1a9660e5c8eada5644b3dcb57ee2346cb84a/LICENSE).

woa.1's change saves `rsp` in `rbp` after saving the registers, aligns `rsp`
to 16 bytes, and restores `rsp` before restoring registers on both exit paths.
It backports commit `8577455` from USVFS PR #93. woa.2's change is in
`src/usvfs_dll/hooks/kernel32.cpp` and `src/usvfs_dll/hookmanager.cpp`, with
the `-woa.2` version suffix. Its release workflow also builds branch pushes,
which produce the archive without publishing it.

The source includes
[the complete build workflow](https://github.com/skjiisa/usvfs/blob/b06d1a9660e5c8eada5644b3dcb57ee2346cb84a/.github/workflows/release-backport.yml).
To rebuild, check out the pinned commit with submodules on Windows with Visual
Studio 2022, obtain the Boost 1.85.0 prebuilt dependency listed in that workflow,
and run its MSBuild commands for x64 and x86. Compiler versions and timestamps
can change the output, so inspect and test rebuilt binaries before updating
ModSync's pinned hashes.

## Validation

### woa.2

Tested on the same Steam Frame on 2026-10-25 with Proton 11.0 (ARM64), an MO2
2.5.2-woa.1 instance installed by MO2-LINT 7.0.3, SKSE 2.2.6 for Skyrim
1.6.1170, and a mod list with three SKSE plugins:

- **SKSE through MO2.** With woa.1, MO2 injected `skse64_loader.exe` but never
  the `SkyrimSE.exe` it started. SKSE found no plugins and the game ran without
  mods. With woa.2, launched through ModSync's Play (`run -e SKSE`), the
  usvfs log showed `CreateProcessA` and `CreateProcessW` hooked, then exactly
  one injection into `SkyrimSE.exe` with `inithooks ... successful`. SKSE's log
  listed the three plugins from MO2's mods, each `loaded correctly`. The game
  reached the title screen without a crash dump.

### woa.1

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
