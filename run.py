"""
run.py — Entry point for the trading bot.

REV 1.0 (2026-10-02) — PHASE 4 MIGRATION:
  Replaces `python app.py`. Flask app now lives at web/app.py;
  templates live at web/templates/.
"""
from web.app import main


if __name__ == "__main__":
    main()