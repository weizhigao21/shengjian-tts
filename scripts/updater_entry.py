"""Small, standalone updater executable; deliberately independent of Qt/backend."""
from multiprocessing import freeze_support

if __name__ == '__main__':
    freeze_support()
    from voice_workbench.app_updates.apply import main
    raise SystemExit(main())
