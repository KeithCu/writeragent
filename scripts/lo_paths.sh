#!/usr/bin/env bash
# Shared LibreOffice profile and binary path helpers (source, do not execute).
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lo_paths.sh"

[[ -n "${_LO_PATHS_LOADED:-}" ]] && return 0
_LO_PATHS_LOADED=1

_lo_macos_program_dir() {
    local macos_bin="/Applications/LibreOffice.app/Contents/MacOS"
    if [[ -d "$macos_bin" ]]; then
        echo "$macos_bin"
        return 0
    fi
    local soffice_path=""
    soffice_path=$(command -v soffice 2>/dev/null || true)
    if [[ -n "$soffice_path" ]]; then
        local resolved macos_dir
        resolved=$(readlink -f "$soffice_path" 2>/dev/null || realpath "$soffice_path" 2>/dev/null || echo "$soffice_path")
        macos_dir=$(dirname "$resolved")
        if [[ -x "$macos_dir/soffice" || -x "$macos_dir/unopkg" ]]; then
            echo "$macos_dir"
            return 0
        fi
    fi
    return 1
}

lo_profile_search_roots() {
    local roots=()
    roots+=("$HOME/.config/libreoffice")
    roots+=("$HOME/Library/Application Support/LibreOffice")
    local snap_data="$HOME/snap/libreoffice/current/.config/libreoffice"
    if [[ -d "$snap_data" ]]; then
        roots+=("$snap_data")
    fi
    local flatpak_data="$HOME/.var/app/org.libreoffice.LibreOffice/config/libreoffice"
    if [[ -d "$flatpak_data" ]]; then
        roots+=("$flatpak_data")
    fi
    printf '%s\n' "${roots[@]}"
}

lo_user_conf_dir() {
    local root conf
    while IFS= read -r root; do
        [[ -z "$root" ]] && continue
        conf="$root/4"
        if [[ -d "$conf" ]]; then
            echo "$conf"
            return 0
        fi
    done < <(lo_profile_search_roots)

    if [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
        echo "$HOME/Library/Application Support/LibreOffice/4"
    else
        echo "${XDG_CONFIG_HOME:-$HOME/.config}/libreoffice/4"
    fi
}

find_soffice() {
    local candidate macos_dir
    for candidate in \
        /usr/bin/soffice \
        /usr/lib/libreoffice/program/soffice \
        /usr/lib64/libreoffice/program/soffice \
        /opt/libreoffice*/program/soffice \
        /snap/bin/libreoffice.soffice \
        /usr/local/bin/soffice; do
        for c in $candidate; do
            if [[ -x "$c" ]]; then
                echo "$c"
                return 0
            fi
        done
    done
    if macos_dir=$(_lo_macos_program_dir); then
        if [[ -x "$macos_dir/soffice" ]]; then
            echo "$macos_dir/soffice"
            return 0
        fi
    fi
    command -v soffice 2>/dev/null || true
}

find_unopkg() {
    local candidate macos_dir
    for candidate in \
        /usr/bin/unopkg \
        /usr/lib/libreoffice/program/unopkg \
        /usr/lib64/libreoffice/program/unopkg \
        /opt/libreoffice*/program/unopkg \
        /snap/bin/libreoffice.unopkg; do
        for c in $candidate; do
            if [[ -x "$c" ]]; then
                echo "$c"
                return 0
            fi
        done
    done
    if macos_dir=$(_lo_macos_program_dir); then
        if [[ -x "$macos_dir/unopkg" ]]; then
            echo "$macos_dir/unopkg"
            return 0
        fi
    fi
    command -v unopkg 2>/dev/null || true
}

find_unopkg_cache_dir() {
    local candidates=()
    candidates+=("$HOME/.config/libreoffice/4/user/uno_packages")
    candidates+=("$HOME/Library/Application Support/LibreOffice/4/user/uno_packages")

    local profile_dir
    while IFS= read -r profile_dir; do
        [[ -z "$profile_dir" ]] && continue
        if [[ -d "$profile_dir" ]]; then
            while IFS= read -r -d '' d; do
                candidates+=("$d")
            done < <(find "$profile_dir" -type d -name "uno_packages" -print0 2>/dev/null)
        fi
    done < <(lo_profile_search_roots)

    local c
    for c in "${candidates[@]}"; do
        if [[ -d "$c" ]]; then
            echo "$c"
            return 0
        fi
    done
}

clear_lo_profile_locks() {
    local root conf
    while IFS= read -r root; do
        [[ -z "$root" ]] && continue
        conf="$root/4"
        [[ -d "$conf" ]] || continue
        rm -f "$conf/.lock" "$conf/user/.lock"
        rm -f "$conf/user/extensions/tmp/extensions.pmap"
        rm -rf "$conf/user/extensions/tmp/"*.tmp_ 2>/dev/null || true
    done < <(lo_profile_search_roots)
}
