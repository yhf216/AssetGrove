import multiprocessing
import sys

if __name__ == '__main__':
    # Frozen multiprocessing must dispatch before importing the web server stack.
    multiprocessing.freeze_support()
    if '--provider-worker' in sys.argv:
        from backend.providers import packaged_worker
        packaged_worker()
    else:
        from backend.server import main
        main()
