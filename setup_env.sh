#!/usr/bin/env bash
# Set up the Python environment for analysis_tools.
#
# Usage:
#   ./setup_env.sh            # use conda/mamba if available, otherwise a venv
#   ./setup_env.sh --venv     # force a plain venv (.venv next to this script)
#   ./setup_env.sh --conda    # force conda/mamba
#   ./setup_env.sh --force    # delete and recreate the environment
#
# Works on Linux, macOS and Windows (Git Bash).

set -euo pipefail

ENV_NAME="analysis_tools"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/environment.yml"
VENV_DIR="$SCRIPT_DIR/.venv"
PIP_PACKAGES=(numpy pandas matplotlib openpyxl tkinterdnd2)
LOCAL_MODULES=(multi_file_analyzer.py trace_store.py chart_tools.py noise.py settings.py smoothing_function.py)
MIN_PY_MINOR=9   # Python 3.9+

MODE="auto"
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --venv)  MODE="venv" ;;
        --conda) MODE="conda" ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Python snippet that checks every third-party import the tools rely on.
VERIFY_CODE='
import sys, tkinter, numpy, pandas
print("python      ", sys.version.split()[0])
print("tk          ", tkinter.TkVersion)
print("numpy       ", numpy.__version__)
print("pandas      ", pandas.__version__)
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.widgets import SpanSelector
    print("matplotlib  ", matplotlib.__version__)
except Exception as e:
    print("matplotlib   MISSING (charts disabled):", e)
try:
    import openpyxl
    print("openpyxl    ", openpyxl.__version__)
except Exception as e:
    print("openpyxl     MISSING (Excel workbooks cannot be opened):", e)
try:
    import tkinterdnd2
    print("tkinterdnd2  ok")
except Exception as e:
    print("tkinterdnd2  MISSING (drag and drop disabled):", e)
'

# ---------------------------------------------------------------- conda ----
setup_conda() {
    local CONDA
    CONDA="$(command -v mamba || command -v conda || true)"
    [[ -n "$CONDA" ]] || die "conda/mamba not found on PATH."
    [[ -f "$ENV_FILE" ]] || die "environment.yml not found at $ENV_FILE"

    if "$CONDA" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
        if [[ $FORCE -eq 1 ]]; then
            info "Removing existing conda env '$ENV_NAME'"
            "$CONDA" env remove -y -n "$ENV_NAME"
            info "Creating conda env '$ENV_NAME'"
            "$CONDA" env create -f "$ENV_FILE"
        else
            info "Updating existing conda env '$ENV_NAME'"
            "$CONDA" env update -n "$ENV_NAME" -f "$ENV_FILE" --prune
        fi
    else
        info "Creating conda env '$ENV_NAME' with $(basename "$CONDA")"
        "$CONDA" env create -f "$ENV_FILE"
    fi

    info "Verifying imports"
    "$CONDA" run -n "$ENV_NAME" python -c "$VERIFY_CODE"

    echo
    info "Done. Activate with:  conda activate $ENV_NAME"
}

# ----------------------------------------------------------------- venv ----
setup_venv() {
    local PY
    PY="$(command -v python3 || command -v python || true)"
    [[ -n "$PY" ]] || die "No python3/python found on PATH."

    "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" \
        || die "Python 3.$MIN_PY_MINOR+ required, found $("$PY" --version 2>&1)."

    # tkinter can't be pip-installed; it must come with the base Python.
    if ! "$PY" -c "import tkinter" 2>/dev/null; then
        warn "tkinter is not available in $("$PY" --version 2>&1)."
        echo "  Debian/Ubuntu: sudo apt install python3-tk"
        echo "  Fedora:        sudo dnf install python3-tkinter"
        echo "  macOS (brew):  brew install python-tk"
        echo "  Windows:       re-run the python.org installer and tick 'tcl/tk and IDLE'"
        die "Install tkinter, then re-run this script."
    fi

    if [[ -d "$VENV_DIR" && $FORCE -eq 1 ]]; then
        info "Removing existing venv at $VENV_DIR"
        rm -rf "$VENV_DIR"
    fi
    if [[ ! -d "$VENV_DIR" ]]; then
        info "Creating venv at $VENV_DIR"
        "$PY" -m venv "$VENV_DIR"
    fi

    # Windows venvs use Scripts/, everything else uses bin/
    local BIN="$VENV_DIR/bin"
    [[ -d "$VENV_DIR/Scripts" ]] && BIN="$VENV_DIR/Scripts"
    local VPY="$BIN/python"

    info "Installing packages: ${PIP_PACKAGES[*]}"
    "$VPY" -m pip install --upgrade pip
    "$VPY" -m pip install --upgrade "${PIP_PACKAGES[@]}"

    info "Verifying imports"
    "$VPY" -c "$VERIFY_CODE"

    echo
    if [[ "$BIN" == *Scripts ]]; then
        info "Done. Activate with:  source \"$VENV_DIR/Scripts/activate\""
    else
        info "Done. Activate with:  source \"$VENV_DIR/bin/activate\""
    fi
}

# ----------------------------------------------------------------- main ----
for f in "${LOCAL_MODULES[@]}"; do
    [[ -f "$SCRIPT_DIR/$f" ]] || warn "$f is missing from $SCRIPT_DIR; the analyzer will not start without it."
done

case "$MODE" in
    conda) setup_conda ;;
    venv)  setup_venv ;;
    auto)
        if command -v mamba >/dev/null 2>&1 || command -v conda >/dev/null 2>&1; then
            setup_conda
        else
            info "conda not found, falling back to a venv"
            setup_venv
        fi
        ;;
esac
