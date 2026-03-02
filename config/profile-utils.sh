#!/bin/bash
# =============================================================================
# Profile Resolution Utilities
# =============================================================================
# Shared helpers for start-dev.sh and start-federated-dev.sh.
# Source this file after setting SCRIPT_DIR.
#
# Provides:
#   source_env_no_override <file>   - Source .env without overriding existing vars
#   resolve_config <profile> <file> - Resolve config file with fallback to default
#   validate_profile <profile>      - Validate profile directory exists
#   apply_profile_env <profile>     - Source .env files with full fallback chain
# =============================================================================

CONFIG_BASE_DIR="$SCRIPT_DIR/config"
DEFAULT_PROFILE_DIR="$CONFIG_BASE_DIR/default"

# Source a .env file, only setting variables not already in the environment.
# This ensures: caller env vars > profile .env > default .env > root .env
source_env_no_override() {
    local env_file="$1"
    [ ! -f "$env_file" ] && return
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        # Skip lines without =
        [[ "$line" != *=* ]] && continue
        # Parse key=value
        local key="${line%%=*}"
        local value="${line#*=}"
        # Strip whitespace from key
        key="$(echo "$key" | tr -d '[:space:]')"
        # Skip empty keys
        [ -z "$key" ] && continue
        # Strip surrounding quotes from value
        value="$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^["'"'"']//;s/["'"'"']$//')"
        # Only set if not already defined in environment
        if [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < "$env_file"
}

# Resolve a config file path: check profile dir first, then default dir.
# Usage: resolve_config <profile_name> <filename>
# Prints the resolved path, or empty string if not found.
resolve_config() {
    local profile_name="$1"
    local filename="$2"
    local profile_dir="$CONFIG_BASE_DIR/$profile_name"

    if [ "$profile_name" != "default" ] && [ -f "$profile_dir/$filename" ]; then
        echo "$profile_dir/$filename"
    elif [ -f "$DEFAULT_PROFILE_DIR/$filename" ]; then
        echo "$DEFAULT_PROFILE_DIR/$filename"
    else
        echo ""
    fi
}

# Validate that a profile directory exists. On failure, lists available profiles
# and exits with code 1.
validate_profile() {
    local profile_name="$1"
    local profile_dir="$CONFIG_BASE_DIR/$profile_name"

    if [ ! -d "$profile_dir" ]; then
        echo -e "${RED:-}Error: Profile '$profile_name' not found at $profile_dir${NC:-}"
        echo "Available profiles:"
        for dir in "$CONFIG_BASE_DIR"/*/; do
            [ -d "$dir" ] && echo "  $(basename "$dir")"
        done
        exit 1
    fi
}

# Apply the full .env fallback chain for a profile:
#   1. Profile .env (highest file priority)
#   2. Default profile .env (fills gaps)
#   3. Root .env (lowest file priority)
# Pre-existing env vars from the caller are never overridden.
apply_profile_env() {
    local profile_name="$1"
    local profile_dir="$CONFIG_BASE_DIR/$profile_name"

    if [ "$profile_name" != "default" ]; then
        source_env_no_override "$profile_dir/.env"
    fi
    source_env_no_override "$DEFAULT_PROFILE_DIR/.env"
    source_env_no_override "$SCRIPT_DIR/.env"
}
