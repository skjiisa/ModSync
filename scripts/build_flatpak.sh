#!/usr/bin/env bash
# Build the ModSync Flatpak from this checkout and install it for the current
# user. Run it from anywhere:
#
#   scripts/build_flatpak.sh                    # build and install
#   scripts/build_flatpak.sh --no-install       # build only
#   scripts/build_flatpak.sh --bundle out.flatpak   # also write a bundle to copy to another machine
#
# Any other argument goes to flatpak-builder as is.
#
# flatpak-builder caches each module it builds. With --state-dir it files that
# cache under the manifest's absolute path, so a second checkout or git
# worktree starts with an empty cache and recompiles 7-Zip and xdelta3.
# Without --state-dir it files the cache under the manifest's file name and
# keeps its state in the current directory. This script therefore always runs
# flatpak-builder from one fixed directory. Every checkout shares the cache,
# and a build after a source change only rebuilds the last module.
set -euo pipefail

APP_ID=io.github.skjiisa.ModSync
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
manifest=$repo_root/packaging/flatpak/$APP_ID.yaml
cache=${MODSYNC_FLATPAK_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/modsync-flatpak}

install=1
bundle=
# The branch and architecture of the build, for --bundle to export the same ref.
branch=master
arch=
passthrough=()
while [ $# -gt 0 ]; do
    case $1 in
        --no-install) install=0 ;;
        --bundle)
            [ $# -ge 2 ] || { echo "--bundle needs a file name" >&2; exit 2; }
            bundle=$(realpath -m -- "$2")
            shift
            ;;
        --bundle=*) bundle=$(realpath -m -- "${1#--bundle=}") ;;
        -h | --help)
            sed -n '2,9s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        --default-branch=*)
            branch=${1#--default-branch=}
            passthrough+=("$1")
            ;;
        --arch=*)
            arch=${1#--arch=}
            passthrough+=("$1")
            ;;
        --default-branch | --arch)
            [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
            passthrough+=("$1=$2")
            if [ "$1" = --arch ]; then arch=$2; else branch=$2; fi
            shift
            ;;
        *) passthrough+=("$1") ;;
    esac
    shift
done

if command -v flatpak-builder > /dev/null; then
    builder=(flatpak-builder)
elif flatpak info org.flatpak.Builder > /dev/null 2>&1; then
    builder=(flatpak run org.flatpak.Builder)
else
    echo "flatpak-builder not found. Install your distribution's flatpak-builder package," >&2
    echo "or: flatpak install flathub org.flatpak.Builder" >&2
    exit 1
fi

mkdir -p "$cache"
cd "$cache"
cache=$PWD

# Builds share the state and build directories, so they run one at a time.
exec 9> "$cache/.lock"
if ! flock --nonblock 9; then
    echo "Another ModSync Flatpak build is running. Waiting for it to finish."
    flock 9
fi

args=(--user --force-clean)
[ "$install" = 1 ] && args+=(--install)
[ -n "$bundle" ] && args+=(--repo="$cache/repo")

start=$SECONDS
"${builder[@]}" "${args[@]}" "${passthrough[@]}" build "$manifest"

if [ -n "$bundle" ]; then
    flatpak build-bundle ${arch:+--arch="$arch"} \
        --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
        "$cache/repo" "$bundle" "$APP_ID" "$branch"
    echo "Wrote $bundle"
fi

echo "Done in $((SECONDS - start)) s."
