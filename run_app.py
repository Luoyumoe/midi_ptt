"""PyInstaller-friendly executable entry point."""

from midi_ptt_controller.main import main


if __name__ == "__main__":
    raise SystemExit(main())
