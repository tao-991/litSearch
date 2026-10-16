#!/bin/bash
# Double-click this file (macOS) to open Lit Search in your browser.
cd "$(dirname "$0")" || exit 1
exec uv run litsearch ui
